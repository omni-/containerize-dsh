"""Discard lifecycle with a stateful fake Docker daemon; no real tasks touched."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_task as tasks


class DiscardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = dict(id='disposable-' + 'a' * 32, resolved=dict(name='fixture'),
                         volumes=dict(workspace='fixture_workspace', home='fixture_home'),
                         volume_created=dict(workspace='original', home='original'),
                         container='owned-id', reviews=[], integrations=[dict(status='blocked')])
        self.directory = self.root / self.task['id']
        self.directory.mkdir()
        tasks.save(self.directory, self.task)
        self.volumes = set(self.task['volumes'].values())
        self.containers = {'owned-id'}
        self.removed = []
        self.stopped = []
        self.foreign = False
        self.created = 'original'
        self.fail_volume_remove = False
        for name, replacement in [('docker_list', self.listing), ('state_root', lambda: self.root)]:
            p = patch.object(tasks, name, replacement)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(tasks.dsh, 'run', self.run_docker)
        p.start()
        self.addCleanup(p.stop)
        confirmation = patch('builtins.input', return_value='y')
        confirmation.start()
        self.addCleanup(confirmation.stop)

    def listing(self, *args):
        if args[:2] == ('volume', 'ls'):
            return list(self.volumes)
        if any(arg.startswith('volume=') for arg in args) and self.foreign:
            return ['foreign-id']
        return list(self.containers)

    def run_docker(self, *args, **kwargs):
        if args[:3] == ('docker', 'volume', 'inspect'):
            name = args[3]
            kind = next(k for k, v in self.task['volumes'].items() if v == name)
            return json.dumps([dict(CreatedAt=self.created, Labels={
                'com.docker.compose.project': 'fixture', 'com.docker.compose.volume': kind})])
        if args[:2] == ('docker', 'inspect'):
            return json.dumps([dict(Id=args[2])])
        if args[:2] == ('docker', 'stop'):
            self.stopped.append(args[2])
        elif args[:2] == ('docker', 'rm'):
            self.containers.remove(args[2])
            self.removed.append(args[2])
        elif args[:3] == ('docker', 'volume', 'rm'):
            if self.fail_volume_remove:
                raise RuntimeError('volume busy')
            self.volumes.remove(args[3])
            self.removed.append(args[3])
        else:
            self.fail(f'Unexpected Docker mutation: {args}')

    def test_discards_without_review_or_host_access_preserving_records(self):
        bundle = self.directory / 'existing.bundle'
        bundle.write_bytes(b'existing recovery data')
        worktree = self.directory / 'integration'
        worktree.mkdir()
        (worktree / 'dirty').write_text('host work remains')
        with patch.object(tasks, 'latest', side_effect=AssertionError('must not review')), \
                patch.object(tasks, 'host', side_effect=AssertionError('must not access host')):
            tasks.main(['discard', self.task['id']])
            tasks.main(['discard', self.task['id']])
            tasks.main(['cleanup', self.task['id']])
        self.assertEqual(self.removed, ['owned-id', 'fixture_workspace'])
        self.assertEqual(self.stopped, ['owned-id'])
        self.assertEqual(self.volumes, {'fixture_home'})
        self.assertTrue(bundle.exists())
        self.assertEqual((worktree / 'dirty').read_text(), 'host work remains')
        self.assertTrue(json.loads((self.directory / 'manifest.json').read_text())['discarded'])

    def test_foreign_consumer_refuses_before_stopping(self):
        self.foreign = True
        with self.assertRaisesRegex(ValueError, 'unrecorded container'):
            tasks.discard(self.directory, self.task)
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.removed, [])

    def test_confirmation_cancels_without_mutation(self):
        original = (self.directory / 'manifest.json').read_bytes()
        for answer in ('n', '', 'yes', 'unexpected', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                kwargs = dict(side_effect=answer) if isinstance(answer, BaseException) else dict(return_value=answer)
                with patch('builtins.input', **kwargs):
                    tasks.main(['discard', self.task['id']])
                self.assertEqual(self.stopped, [])
                self.assertEqual(self.removed, [])
                self.assertEqual((self.directory / 'manifest.json').read_bytes(), original)

    def test_confirmation_identifies_task_and_accepts_uppercase_y(self):
        with patch('builtins.input', return_value=' Y ') as prompt:
            tasks.main(['discard', self.task['id']])
        self.assertIn(self.task['id'], prompt.call_args.args[0])
        self.assertIn('[y/n]', prompt.call_args.args[0])
        self.assertEqual(self.removed, ['owned-id', 'fixture_workspace'])
        with patch('builtins.input', side_effect=AssertionError('Already discarded must be a no-op')):
            tasks.main(['discard', self.task['id']])

    def test_recreated_volume_refuses_before_stopping(self):
        self.created = 'replacement'
        with self.assertRaisesRegex(ValueError, 'ownership changed'):
            tasks.discard(self.directory, self.task)
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.removed, [])

    def test_foreign_project_container_refuses(self):
        self.containers.add('foreign-id')
        with self.assertRaisesRegex(ValueError, 'namespace'):
            tasks.discard(self.directory, self.task)
        self.assertEqual(self.removed, [])

    def test_failed_volume_removal_can_resume_but_task_cannot_restart(self):
        self.fail_volume_remove = True
        with self.assertRaisesRegex(RuntimeError, 'volume busy'):
            tasks.main(['discard', self.task['id']])
        with self.assertRaisesRegex(ValueError, 'Discard has started'):
            tasks.main(['resume', self.task['id']])
        self.fail_volume_remove = False
        tasks.main(['discard', self.task['id']])
        self.assertEqual(self.removed, ['owned-id', 'fixture_workspace'])

    def test_retry_after_resources_removed_before_final_save(self):
        self.task['discard_started'] = True
        self.containers.clear()
        self.volumes.remove('fixture_workspace')
        tasks.discard(self.directory, self.task)
        self.assertTrue(self.task['discarded'])
        self.assertEqual(self.removed, [])


if __name__ == '__main__':
    unittest.main()
