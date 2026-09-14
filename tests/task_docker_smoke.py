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
        registry.write_text(json.dumps({'synthetic': dict(repo=str(repo), port=port)}))
        task = None
        with patch.object(tasks.dsh, 'profiles_path', return_value=registry), \
                patch.object(tasks.dsh.Sandbox, 'build', side_effect=AssertionError('Must reuse cached image')):
            try:
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
