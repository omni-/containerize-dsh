"""Opt-in smoke test: cached image, synthetic repo, unique resources, no model calls."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_task as tasks
from git_transfer import git


def main():
    os.environ.pop('DEEPSEEK_API_KEY', None)
    with tempfile.TemporaryDirectory(prefix='dsh-task-smoke-') as temp:
        root = Path(temp)
        repo = root / 'synthetic-repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@localhost')
        (repo / 'file').write_text('base\n')
        (repo / '.gitignore').write_text('ignored\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        # A dirty baseline unrelated to the agent delta must stay uncommitted.
        (repo / 'baseline').write_text('user-owned baseline\n')
        index = (repo / '.git/index').read_bytes()
        registry = root / 'config' / 'profiles.json'
        registry.parent.mkdir()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        profile = dict(repo=str(repo), port=port)
        if '--headless' in sys.argv:
            plugin = root / 'headless-plugin'
            runtime = plugin / 'runtime'
            runtime.mkdir(parents=True)
            (plugin / 'plugin.json').write_text(json.dumps(dict(version=1, runtime='runtime')))
            (runtime / 'bootstrap.sh').write_text(
                'set -eu\nprintf "bootstrap\\n" >> /home/agent/headless-order\nprintf "hook output\\n"\n', newline='\n')
            (runtime / 'loaded.mjs').write_text(
                "import { appendFileSync } from 'node:fs';\n"
                "export const name = 'smoke-headless';\n"
                "export function apply() { appendFileSync('/home/agent/headless-order', 'cordis\\n'); }\n", newline='\n')
            (runtime / 'cordis.yml').write_text(
                '- insert:\n    - id: smoke-headless\n      name: ./loaded.mjs\n', newline='\n')
            profile['plugins'] = [str(plugin)]
        registry.write_text(json.dumps({'synthetic': profile}))
        task = None
        with patch.object(tasks.dsh, 'profiles_path', return_value=registry), \
                patch.object(tasks.dsh.Sandbox, 'build', side_effect=AssertionError('Must reuse cached image')):
            try:
                if '--headless' in sys.argv:
                    prompt = root / 'prompt.txt'
                    prompt.write_text('Describe the file in this repository.\n', encoding='utf-8')
                    with redirect_stdout(io.StringIO()) as output:
                        result = tasks.main(['run', '--profile', 'synthetic', '--task', 'headless-smoke',
                            '--prompt-file', str(prompt), '--snapshot', '--include-untracked',
                            '--effort', 'high', '--timeout', '60'])
                    records = list(tasks.state_root().glob('*/manifest.json'))
                    assert len(records) == 1
                    directory = records[0].parent
                    task = json.loads(records[0].read_text())
                    run = task['headless']
                    assert result == 1 and run['exit_code'] != 0 and not run['timed_out'], run
                    assert run['started_at'] and run['ended_at']
                    assert output.getvalue().startswith('Task ID: ' + task['id'] + '\n')
                    assert '?token=' not in output.getvalue()
                    events = [json.loads(line) for line in Path(run['events']).read_text(encoding='utf-8').splitlines()]
                    assert events and events[-1]['type'] in ('error', 'final'), events
                    assert run['error'] or run['final_reason'] == 'error', run
                    # Reject a CLI/configuration failure masquerading as a credential failure.
                    failure = json.dumps(events[-1]).lower()
                    assert any(word in failure for word in ('key', 'credential', 'authentication', 'sign in')), failure
                    info = json.loads(tasks.dsh.run('docker', 'inspect', task['container'], capture=True))[0]
                    assert not info['State']['Running']
                    assert not info['HostConfig']['PortBindings']
                    assert info['HostConfig']['ReadonlyRootfs'] and info['HostConfig']['CapDrop'] == ['ALL']
                    assert 'no-new-privileges:true' in info['HostConfig']['SecurityOpt']
                    assert info['Config']['User'] == '10001:10001'
                    assert info['Config']['Cmd'] == ['/opt/dsh/start-dsh.sh', 'headless']
                    assert (repo / '.git/index').read_bytes() == index
                    with tasks.sandbox(task) as box, box.maintenance() as container:
                        assert tasks.dsh.run('docker', 'exec', container, 'cat',
                            '/home/agent/headless-order', capture=True) == 'bootstrap\ncordis'
                    bundle = root / 'headless.bundle'
                    with redirect_stdout(io.StringIO()):
                        tasks.main(['export', task['id'], '--bundle', str(bundle), '--snapshot', '--include-untracked'])
                    assert bundle.is_file()
                    original = task
                    with redirect_stdout(io.StringIO()) as output:
                        tasks.main(['resume', task['id']])
                    task = json.loads(records[0].read_text())
                    assert task['container'] != original['container']
                    assert task['volumes'] == original['volumes'] and task['headless'] == run
                    assert f'http://127.0.0.1:{port}/?token=' in output.getvalue()
                    info = json.loads(tasks.dsh.run('docker', 'inspect', task['container'], capture=True))[0]
                    assert info['Image'] == original['resolved']['image']
                    with tasks.sandbox(task) as box:
                        review = tasks.export_task(directory, task, box)
                        review['report'] = tasks.evidence(directory, prompt, 'fixture-review')
                        tasks.save(directory, task)
                        tasks.cleanup(directory, task, box)
                    print('PASS: key-free headless runner and credential failure, Cordis/bootstrap order, effort overlay, event/outcome recording, isolation, direct/review export, web resume, cleanup and home retention.')
                    return
                output = io.StringIO()
                with redirect_stdout(output):
                    tasks.main(['start', '--profile', 'synthetic', '--task', 'smoke', '--snapshot', '--include-untracked'])
                records = list(tasks.state_root().glob('*/manifest.json'))
                assert len(records) == 1
                directory = records[0].parent
                task = json.loads(records[0].read_text())
                assert f'http://127.0.0.1:{port}/?token=' in output.getvalue()
                assert task['base'] != task['source_head']
                assert (repo / '.git/index').read_bytes() == index
                assert (repo / 'baseline').read_text() == 'user-owned baseline\n'
                cid = task['container']
                tasks.dsh.run('docker', 'exec', cid, 'sh', '-ec',
                    'printf "agent change\\n" > /workspace/file; printf "new agent file\\n" > /workspace/new')
                if '--discard' in sys.argv:
                    tasks.dsh.run('docker', 'exec', cid, 'sh', '-ec',
                        'printf disposable > /workspace/ignored; printf retained > /home/agent/discard-history')
                    with patch('builtins.input', return_value='y'):
                        tasks.main(['discard', task['id']])
                        tasks.main(['discard', task['id']])
                    saved = json.loads(records[0].read_text())
                    assert saved['discarded'] and not saved['reviews']
                    volumes = tasks.docker_list('volume', 'ls', '--format', '{{.Name}}')
                    assert task['volumes']['workspace'] not in volumes
                    assert task['volumes']['home'] in volumes
                    assert (repo / '.git/index').read_bytes() == index
                    assert (repo / 'baseline').read_text() == 'user-owned baseline\n'
                    assert tasks.dsh.run('docker', 'run', '--rm', '--entrypoint', 'cat',
                        '--mount', 'type=volume,src=' + task['volumes']['home'] + ',dst=/retained,readonly',
                        'dsh-sandbox:local', '/retained/discard-history', capture=True) == 'retained'
                    print('PASS: discard running disposable task with dirty/new/ignored files, no review, repeat-safe, host and home preserved.')
                    return
                with tasks.sandbox(task) as box:
                    try:
                        tasks.export_task(directory, task, box)
                    except ValueError as error:
                        assert '--include-untracked' in str(error)
                    else:
                        raise AssertionError('New files must require explicit inclusion')
                    first = tasks.export_task(directory, task, box, True)
                    second = tasks.export_task(directory, task, box, True)
                    assert first['branch'] != second['branch']
                    second['report'] = tasks.evidence(directory, registry, 'fixture-review')
                    tasks.save(directory, task)
                    prepared = tasks.prepare(directory, task)
                    assert git(prepared['worktree'], 'ls-files', 'baseline') == ''
                    assert (Path(prepared['worktree']) / 'new').read_text() == 'new agent file\n'
                    assert (repo / '.git/index').read_bytes() == index
                    # The untracked baseline blocks finalize; preserve it outside the checkout.
                    try:
                        tasks.finalize(directory, task, registry)
                    except ValueError as error:
                        assert 'Integration is incomplete' in str(error)
                    else:
                        raise AssertionError('Dirty target must block finalize')
                    (repo / 'baseline').rename(root / 'preserved-baseline')
                    tasks.finalize(directory, task, registry)
                    tasks.cleanup(directory, task, box, require_integrated=True)
                    tasks.cleanup(directory, task, box, require_integrated=True)
                volumes = tasks.docker_list('volume', 'ls', '--format', '{{.Name}}')
                assert task['volumes']['home'] in volumes
                assert task['volumes']['workspace'] not in volumes
                assert Path(second['bundle']).exists()
                assert not Path(prepared['worktree']).exists()
                assert (root / 'preserved-baseline').read_text() == 'user-owned baseline\n'
                print('PASS: cached-image start and URL, snapshot isolation, new-file refusal, repeated review, agent-only integration, dirty-target refusal, cleanup and home retention.')
            finally:
                # Only this fixture's explicitly recorded resources, even on failure.
                if task is None:
                    records = list(tasks.state_root().glob('*/manifest.json'))
                    if records:
                        task = json.loads(records[0].read_text())
                if task:
                    with tasks.sandbox(task) as box:
                        box.compose('down')
                    names = tasks.docker_list('volume', 'ls', '--format', '{{.Name}}')
                    for name in task['volumes'].values():
                        if name in names:
                            tasks.dsh.run('docker', 'volume', 'rm', name)


if __name__ == '__main__':
    main()
