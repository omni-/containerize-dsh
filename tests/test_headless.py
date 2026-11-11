"""Headless task fixtures: real temporary Git repositories, no Docker or provider calls."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh
import dsh_task as tasks
from git_transfer import git
import test_hardening as hardening


def ndjson(*events):
    return b''.join((json.dumps(event) + '\n').encode('utf-8') for event in events)


SESSION = dict(type='session', sessionId='session-fixture', cwd='/workspace')
COMPLETED = dict(type='final', text='Done', reason=dict(kind='completed'))


class HeadlessArgumentsTests(unittest.TestCase):
    def test_run_options_and_defaults(self):
        with patch.object(tasks, 'run_task', return_value=0) as run:
            self.assertEqual(tasks.main(['run', '--task', 'fixture', '--prompt-file', '-']), 0)
        args = run.call_args.args[0]
        self.assertEqual((args.action, args.task, args.prompt_file, args.revision),
                         ('run', 'fixture', '-', 'HEAD'))
        self.assertIsNone(args.events)
        self.assertIsNone(args.timeout)
        self.assertIsNone(args.model)
        self.assertIsNone(args.effort)
        with patch.object(tasks, 'run_task', return_value=1) as run:
            self.assertEqual(tasks.main(['run', '--task', 'fixture', '--prompt-file', 'prompt.txt',
                '--profile', 'saved', '--repo', 'repo', '--target', 'main', '--revision', 'HEAD',
                '--snapshot', '--include-untracked', '--rebuild', '--events', 'events.jsonl',
                '--timeout', '1.5', '--model', 'provider/model', '--effort', 'high']), 1)
        args = run.call_args.args[0]
        self.assertEqual((args.profile, args.repo, args.target, args.events, args.timeout,
                          args.model, args.effort),
                         ('saved', 'repo', 'main', 'events.jsonl', 1.5, 'provider/model', 'high'))
        self.assertTrue(args.snapshot and args.include_untracked and args.rebuild)

    def test_invalid_arguments_never_start_setup(self):
        cases = [[], ['--include-untracked'], ['--port', '12345'], ['--open'],
                 ['--model', ' '], ['--effort', ''], ['--model', '/model'], ['--model', 'provider/'],
                 *[['--timeout', value] for value in ('0', '-1', 'nan', 'inf', '-inf', 'bad')]]
        for options in cases:
            argv = ['run', '--task', 'fixture']
            if options:
                argv += ['--prompt-file', '-', *options]
            with self.subTest(options=options), redirect_stderr(io.StringIO()), \
                    patch.object(tasks, 'run_task') as run, self.assertRaises(SystemExit) as error:
                tasks.main(argv)
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_prompt_file_and_stdin_preserve_utf8_and_newlines(self):
        content = 'Fix the parser: \u03bb\r\nKeep the trailing newline.\n'.encode('utf-8')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'prompt.txt'
            path.write_bytes(content)
            self.assertEqual(tasks.read_prompt(str(path)), content)
        with patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(content), encoding='utf-8')):
            self.assertEqual(tasks.read_prompt('-'), content)

    def test_invalid_prompt_fails_before_resources(self):
        for content in (b'', b' \r\n\t', b'\xff'):
            with self.subTest(content=content), \
                    patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(content), encoding='utf-8')), \
                    patch.object(tasks, 'setup_task') as setup, self.assertRaises(ValueError):
                tasks.main(['run', '--task', 'fixture', '--prompt-file', '-'])
            setup.assert_not_called()


class HeadlessTaskTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@localhost')
        (self.repo / 'file').write_text('base\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'base')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.prompt = b'private prompt sentinel\n'
        self.prompt_file = self.root / 'prompt.txt'
        self.prompt_file.write_bytes(self.prompt)
        self.box = Mock()
        self.box.env = dict(DSH_IMAGE='fixture-image', DEEPSEEK_API_KEY='private-key-sentinel')
        self.cid = 'c' * 64
        self.image = 'sha256:' + 'd' * 64
        self.box.compose.side_effect = lambda *args, **kw: self.cid if args[0] == 'ps' else None
        self.docker = []
        self.exit_code = 0
        self.events = ndjson(SESSION, COMPLETED)
        self.timeout = False
        self.client_code = None
        self.interrupted = False
        self.process = None

        def inspect(*args, **kwargs):
            self.docker.append(args)
            if args[:3] == ('docker', 'volume', 'inspect'):
                return '[{"CreatedAt":"fixture-created"}]'
            if args[:2] == ('docker', 'inspect'):
                return json.dumps([dict(Id=self.cid, Image=self.image,
                                        State=dict(Running=False, ExitCode=self.exit_code))])
            if args[:2] in (('docker', 'stop'), ('docker', 'rm')):
                return self.cid
            raise AssertionError(args)

        test = self

        class PromptPipe(io.BytesIO):
            def close(self):
                test.received_prompt = self.getvalue()
                super().close()

        def launch(command, *, stdin, stdout, stderr):
            self.launch_command = command
            self.launch_stderr = stderr
            self.assertIs(stdin, subprocess.PIPE)
            stdout.write(self.events)
            stdout.flush()
            self.streamed_before_wait = True
            waits = 0
            process = Mock()
            process.returncode = None
            process.stdin = PromptPipe()

            def wait(timeout=None):
                nonlocal waits
                waits += 1
                if waits == 1 and self.timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                if waits == 1 and self.interrupted:
                    raise KeyboardInterrupt()
                process.returncode = self.exit_code if self.client_code is None else self.client_code
                return process.returncode

            process.wait.side_effect = wait
            process.poll.side_effect = lambda: process.returncode
            self.process = process
            return process

        self.launch = Mock(side_effect=launch)
        local_subprocess = SimpleNamespace(Popen=self.launch, DEVNULL=subprocess.DEVNULL, PIPE=subprocess.PIPE,
            CalledProcessError=subprocess.CalledProcessError, TimeoutExpired=subprocess.TimeoutExpired)
        patches = [
            patch.object(tasks, 'state_root', return_value=self.root / 'tasks'),
            patch.object(tasks, 'sandbox'),
            patch.object(tasks, 'docker_list', side_effect=lambda *args: ['fixture-image'] if args[0] == 'image' else []),
            patch.object(tasks, 'audit', return_value=dict(base=self.head, host=self.head)),
            patch.object(dsh, 'transfer'), patch.object(dsh, 'run', side_effect=inspect),
            patch.object(tasks, 'show_url'), patch.object(tasks, 'owned'),
            patch.object(tasks, 'subprocess', local_subprocess),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.sandbox, self.transfer, self.show_url, self.owned = mocks[1], mocks[4], mocks[6], mocks[7]
        self.sandbox.return_value.__enter__.return_value = self.box

    def invoke(self, *options):
        self.output = io.StringIO()
        with redirect_stdout(self.output):
            result = tasks.main(['run', '--task', 'Fixture', '--repo', str(self.repo),
                                 '--prompt-file', str(self.prompt_file), *options])
        manifests = list(tasks.state_root().glob('*/manifest.json'))
        self.assertEqual(len(manifests), 1)
        self.directory = manifests[0].parent
        self.task = json.loads(manifests[0].read_text())
        self.assertFalse((self.directory / 'lock').exists())
        return result

    def test_completed_run_streams_privately_and_records_same_setup(self):
        self.events = ndjson(SESSION, dict(type='status', phase='turn_start'),
            dict(type='thinking', text='Thinking'), dict(type='text', text='Working'),
            dict(type='tool_call', callId='one', tool='bash', input={}),
            dict(type='tool_result', callId='one', status='success', result='ok'), COMPLETED)
        self.assertEqual(self.invoke('--timeout', '30'), 0)
        run = self.task['headless']
        self.assertEqual(self.received_prompt, self.prompt)
        self.assertTrue(self.streamed_before_wait)
        self.assertEqual(self.launch_command, ['docker', 'start', '--attach', '--interactive', self.cid])
        self.assertEqual(self.launch_stderr, subprocess.DEVNULL)
        self.process.wait.assert_called_once_with(timeout=30)
        self.box.headless.assert_called_once_with(None, None, len(self.prompt))
        self.box.compose.assert_any_call('create', '--no-build', 'dsh')
        self.box.compose.assert_any_call('ps', '-aq', 'dsh', capture=True)
        self.assertNotIn('up', [call.args[0] for call in self.box.compose.call_args_list])
        self.box.preflight.assert_called_once()
        self.box.build.assert_not_called()
        args = self.transfer.call_args.args[1]
        self.assertEqual((args.action, args.repo, args.revision), ('init', str(self.repo), 'HEAD'))
        self.assertEqual((self.task['base'], self.task['transfer_host'], self.task['container']),
                         (self.head, self.head, self.cid))
        self.assertEqual(self.task['resolved']['image'], self.image)
        self.assertEqual((run['exit_code'], run['final_reason'], run['session_id'], run['timed_out'], run['error']),
                         (0, 'completed', 'session-fixture', False, None))
        self.assertLessEqual(run['started_at'], run['ended_at'])
        self.assertEqual(Path(run['events']).read_bytes(), self.events)
        self.assertEqual(Path(run['events']), self.directory / 'events.ndjson')
        self.assertEqual(self.output.getvalue().splitlines()[0], 'Task ID: ' + self.task['id'])
        self.assertIn('Events: ' + run['events'], self.output.getvalue())
        self.show_url.assert_not_called()
        commands = str([call.args for call in self.box.compose.call_args_list] + self.docker + [self.launch_command])
        for private in ('private prompt sentinel', 'private-key-sentinel', '?token='):
            self.assertNotIn(private, commands)
            self.assertNotIn(private, self.output.getvalue())
            self.assertNotIn(private, json.dumps(self.task))
        with redirect_stdout(io.StringIO()) as output:
            tasks.main(['show', self.task['id']])
        self.assertEqual(json.loads(output.getvalue())['headless'], run)

    def test_external_events_stdin_profile_and_start_options(self):
        plugin = self.root / 'plugin'
        plugin.mkdir()
        key = self.root / 'provider-key'
        key.write_text('private-key-sentinel')
        profile = dict(repo=str(self.repo), plugins=[str(plugin)], key_file=str(key), port=12345)
        destination = self.root / 'events.jsonl'
        with patch.object(dsh, 'load_profile', return_value=profile) as load, \
                patch.object(sys, 'stdin', io.TextIOWrapper(io.BytesIO(self.prompt), encoding='utf-8')):
            self.assertEqual(self.invoke('--profile', 'saved', '--prompt-file', '-',
                '--target', 'main', '--snapshot', '--include-untracked', '--rebuild',
                '--events', str(destination), '--model', 'deepseek/deepseek-chat', '--effort', 'high'), 0)
        load.assert_called_once_with('saved')
        self.assertEqual(self.task['resolved']['plugins'], [str(plugin)])
        self.assertEqual(self.task['resolved']['key_file'], str(key))
        self.assertEqual(self.task['resolved']['port'], 12345)
        self.assertEqual(self.task['headless']['events'], str(destination))
        self.assertEqual(self.received_prompt, self.prompt)
        self.box.build.assert_called_once()
        self.box.headless.assert_called_once_with('deepseek/deepseek-chat', 'high', len(self.prompt))
        args = self.transfer.call_args.args[1]
        self.assertTrue(args.snapshot and args.include_untracked)

    def test_failed_final(self):
        self.exit_code = 1
        self.events = ndjson(SESSION, dict(type='final', text='', reason=dict(kind='error', message='No key')))
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.task['headless']['final_reason'], 'error')
        self.assertEqual(self.task['headless']['exit_code'], 1)

    def test_error_event_message(self):
        self.exit_code = 1
        self.events = ndjson(dict(type='error', message='Runner failed to create a session'))
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.task['headless']['error'], 'Runner failed to create a session')
        self.assertIsNone(self.task['headless']['final_reason'])
        self.assertIsNone(self.task['headless']['session_id'])

    def test_timeout_stops_container_and_retains_volumes_even_after_completed_event(self):
        self.timeout = True
        self.exit_code = 143
        self.assertEqual(self.invoke('--timeout', '0.1'), 1)
        self.assertTrue(self.task['headless']['timed_out'])
        self.assertEqual(self.task['headless']['exit_code'], 143)
        self.assertEqual(self.task['headless']['final_reason'], 'completed')
        self.owned.assert_called_once()
        self.assertIn(('docker', 'stop', '--time', '10', self.cid), self.docker)
        self.assertFalse(any('rm' in command for command in self.docker))
        self.assertEqual(set(self.task['volumes']), {'workspace', 'home'})

    def test_interruption_stops_container_and_finishes_record(self):
        self.interrupted = True
        self.exit_code = 143
        self.assertEqual(self.invoke(), 1)
        self.assertIn('interrupted', self.task['headless']['error'])
        self.assertFalse(self.task['headless']['timed_out'])
        self.assertTrue(self.task['headless']['ended_at'])
        self.assertIn(('docker', 'stop', '--time', '10', self.cid), self.docker)

    def test_missing_final_cannot_succeed(self):
        self.events = ndjson(SESSION)
        self.assertEqual(self.invoke(), 1)
        self.assertIn('without a final', self.task['headless']['error'])

    def test_zero_exit_with_failed_final_cannot_succeed(self):
        self.events = ndjson(SESSION, dict(type='final', reason=dict(kind='aborted')))
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.task['headless']['final_reason'], 'aborted')

    def test_completed_final_with_nonzero_exit_cannot_succeed(self):
        self.exit_code = 1
        self.assertEqual(self.invoke(), 1)

    def test_client_failure_after_completed_container_cannot_succeed(self):
        self.client_code = 125
        self.assertEqual(self.invoke(), 1)
        self.assertIn('125', self.task['headless']['error'])

    def test_malformed_stream_cannot_succeed(self):
        self.events = b'not JSON\n' + self.events
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(self.task['headless']['error'], 'Invalid headless event stream')

    def test_empty_error_message_and_final_without_reason_cannot_succeed(self):
        for events in (ndjson(SESSION, dict(type='error', message=''), COMPLETED),
                       ndjson(SESSION, COMPLETED, dict(type='final', text='No reason'))):
            with self.subTest(events=events):
                path = self.root / 'synthetic-events.ndjson'
                path.write_bytes(events)
                outcome = tasks.read_outcome(path)
                self.assertTrue(outcome['error'] or outcome['final_reason'] != 'completed')

    def test_turn_end_reason_decides_when_final_has_only_text(self):
        turn_end = lambda reason: dict(type='status', phase='turn_end', turn=1, reason=reason)
        final = dict(type='final', text='')
        missing_key = dict(kind='error', error=dict(message='no API key', code='MISSING_CREDENTIAL'))
        for events, expected in ((ndjson(SESSION, turn_end(dict(kind='completed')), final), ('completed', None)),
                                 (ndjson(SESSION, turn_end(missing_key), final), ('error', 'no API key'))):
            with self.subTest(expected=expected):
                path = self.root / 'synthetic-events.ndjson'
                path.write_bytes(events)

                outcome = tasks.read_outcome(path)

                self.assertEqual((outcome['final_reason'], outcome['error']), expected)

    def test_timeout_ownership_refusal_never_stops_an_unverified_container(self):
        self.timeout = True
        self.exit_code = 143
        self.owned.side_effect = ValueError('Unrecorded consumer')
        self.assertEqual(self.invoke('--timeout', '0.1'), 1)
        self.assertTrue(self.task['headless']['timed_out'])
        self.assertTrue(self.task['headless']['ended_at'])
        self.owned.assert_called_once()
        self.assertFalse(any(command[:2] == ('docker', 'stop') for command in self.docker))

    def test_existing_events_file_is_never_overwritten(self):
        destination = self.root / 'existing.ndjson'
        destination.write_bytes(b'prior run\n')
        with self.assertRaises(FileExistsError):
            self.invoke('--events', str(destination))
        self.assertEqual(destination.read_bytes(), b'prior run\n')
        self.launch.assert_not_called()

    def test_launch_failure_finishes_record(self):
        self.launch.side_effect = OSError('fixture launch failure')
        self.assertEqual(self.invoke(), 1)
        self.assertTrue(self.task['headless']['ended_at'])
        self.assertIsNone(self.task['headless']['exit_code'])
        self.assertEqual(self.task['container'], self.cid)

    def test_start_still_uses_shared_setup_and_prints_only_original_output(self):
        with patch.object(tasks, 'setup_task', wraps=tasks.setup_task) as setup, redirect_stdout(io.StringIO()) as output:
            tasks.main(['start', '--task', 'fixture', '--repo', str(self.repo), '--port', '12345', '--open'])
        setup.assert_called_once()
        self.box.compose.assert_any_call('up', '-d', '--no-build')
        self.box.headless.assert_not_called()
        self.launch.assert_not_called()
        self.show_url.assert_called_once_with(self.box, 12345, True)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(output.getvalue().startswith('Task ID: fixture-'))

    def test_resume_replaces_headless_with_web_once_using_recorded_volumes_and_image(self):
        self.assertEqual(self.invoke(), 0)
        original = copy.deepcopy(self.task)
        self.box.reset_mock(side_effect=False)
        with redirect_stdout(io.StringIO()):
            tasks.main(['resume', self.task['id']])
        updated = json.loads((self.directory / 'manifest.json').read_text())
        self.assertEqual(updated['headless'], original['headless'])
        self.assertEqual(updated['volumes'], original['volumes'])
        self.assertEqual(updated['resolved']['image'], self.image)
        self.assertEqual(updated['container_mode'], 'web')
        self.assertIn(('docker', 'rm', self.cid), self.docker)
        self.box.compose.assert_any_call('up', '-d', '--no-build', '--pull', 'never', 'dsh')
        self.box.reset_mock(side_effect=False)
        with redirect_stdout(io.StringIO()):
            tasks.main(['resume', self.task['id']])
        self.box.compose.assert_called_once_with('start')

    def test_old_record_shows_and_resumes_without_headless_fields(self):
        self.assertEqual(self.invoke(), 0)
        for key in ('headless', 'container_mode'):
            self.task.pop(key)
        self.task['resolved'].pop('image')
        tasks.save(self.directory, self.task)
        with redirect_stdout(io.StringIO()) as output:
            tasks.main(['show', self.task['id']])
        self.assertNotIn('headless', json.loads(output.getvalue()))
        self.box.reset_mock(side_effect=False)
        with redirect_stdout(io.StringIO()):
            tasks.main(['resume', self.task['id']])
        self.box.compose.assert_called_once_with('start')


class HeadlessComposeTests(unittest.TestCase):
    def test_headless_config_changes_only_command_stdin_ports_and_model_environment(self):
        fixture = hardening.ComposeBoundaryTests()
        fixture.setUp()
        config = fixture.core
        config['services']['dsh']['environment'] = dict(DEEPSEEK_API_KEY='private-key-sentinel',
            DSH_PLUGIN_RUNTIMES='[]', DEEPSEEK_API_KEY_FILE='/run/secrets/deepseek', PLUGIN_SETTING='value')
        with tempfile.TemporaryDirectory() as temp:
            box = object.__new__(dsh.Sandbox)
            box.command = ['docker', 'compose', '-f', str(Path(temp) / 'composed.json')]
            box.base_command = ['docker', 'compose', '--project-name', 'fixture']
            box.compose = Mock(return_value=json.dumps(config))
            box.headless('provider/model', 'high', 12)
            generated = json.loads((Path(temp) / 'headless.json').read_text())
        expected = copy.deepcopy(config)
        service = expected['services']['dsh']
        service.pop('ports')
        service.update(command=['/opt/dsh/start-dsh.sh', 'headless'], stdin_open=True)
        service['environment'].update(DSH_HEADLESS_MODEL='provider/model', DSH_HEADLESS_EFFORT='high',
                                      DSH_PROMPT_BYTES='12')
        self.assertEqual(generated, expected)
        self.assertEqual(box.command[:len(box.base_command)], box.base_command)
        self.assertNotIn('private-key-sentinel', str(box.command))

    def test_recorded_image_is_used_in_sandbox_environment(self):
        fixture = hardening.ComposeBoundaryTests()
        fixture.setUp()
        args = SimpleNamespace(name='fixture', port=11111, plugins=[], key_file=None, image='sha256:fixture')
        with tempfile.TemporaryDirectory() as temp, patch.object(dsh, 'run', return_value=json.dumps(fixture.core)) as run:
            box = dsh.Sandbox(args, temp)
        self.assertEqual(box.env['DSH_IMAGE'], 'sha256:fixture')
        self.assertTrue(all(call.kwargs['env']['DSH_IMAGE'] == 'sha256:fixture' for call in run.call_args_list))

    def test_recorded_image_is_not_replaced_by_a_plugin_tag_on_resume(self):
        fixture = hardening.ComposeBoundaryTests()
        fixture.setUp()
        with tempfile.TemporaryDirectory() as temp:
            plugin = Path(temp) / 'plugin'
            plugin.mkdir()
            (plugin / 'plugin.json').write_text(json.dumps(dict(version=1, build={})))
            (plugin / 'Dockerfile').write_text('ARG DSH_BASE_IMAGE\nFROM ${DSH_BASE_IMAGE}\n')
            args = SimpleNamespace(name='fixture', port=11111, plugins=[str(plugin)],
                                   key_file=None, image='sha256:fixture')
            with patch.object(dsh, 'run', return_value=json.dumps(fixture.core)):
                box = dsh.Sandbox(args, temp)
            self.assertEqual(box.env['DSH_IMAGE'], 'sha256:fixture')
            self.assertTrue(box.builds[0][1].startswith('dsh-plugin:'))


if __name__ == '__main__':
    unittest.main()
