"""Real Docker transfers with synthetic nested submodules and disposable volumes."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_submodules import SubmoduleTests
import dsh
import dsh_task as tasks
from git_transfer import git


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='dsh-sandbox:local')
    args = parser.parse_args()
    fixture = SubmoduleTests()
    fixture.setUp()
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, DEEPSEEK_API_KEY=''))
            temp = stack.enter_context(tempfile.TemporaryDirectory(prefix='dsh-submodule-compose-'))
            name = 'dsh-submodule-smoke-' + uuid.uuid4().hex
            settings = dict(name=name, port=11112, plugins=[], key_file=None)
            box = dsh.Sandbox(argparse.Namespace(action='status', **settings), temp)
            # Select a test image without changing profiles or rebuilding active tasks.
            config_path = Path(temp) / 'composed.json'
            config = json.loads(config_path.read_text(encoding='utf-8'))
            config['services']['dsh']['image'] = args.image
            config_path.write_text(json.dumps(config), encoding='utf-8')
            box.env['DSH_IMAGE'] = args.image
            box.check_config()
            volumes = {kind: config['volumes'][kind]['name'] for kind in ('workspace', 'home')}
            try:
                (fixture.child / 'file').write_text('user baseline edit\n')
                (fixture.leaf / 'supplied-new').write_text('user supplied file\n')
                before = fixture.state()
                dsh.transfer(box, argparse.Namespace(action='init', repo=fixture.host, revision='HEAD',
                                                     snapshot=True, include_untracked=True))
                initial = tasks.audit(box)
                fixture.assert_preserved(before)
                assert fixture.path + '/nested' in initial['submodules']
                with box.maintenance() as container:
                    dsh.run('docker', 'exec', container, 'python3', '-c',
                            'from pathlib import Path; '
                            f'r=Path("/workspace/{fixture.path}/nested"); '
                            'assert (r/"supplied-new").read_text()=="user supplied file\\n"; '
                            '(r/"agent-new").write_text("agent work\\n"); '
                            '(r/"ignored").write_text("ignored cache\\n")')
                directory = fixture.root / 'docker-task'
                directory.mkdir()
                task = dict(id='smoke-' + uuid.uuid4().hex, repo=str(fixture.host),
                            repository=tasks.identity(fixture.host), target='main', resolved=settings,
                            volumes=volumes, reviews=[], base=initial['base'], transfer_host=initial['host'],
                            volume_created={kind: json.loads(dsh.run('docker', 'volume', 'inspect', volume,
                                                capture=True))[0]['CreatedAt'] for kind, volume in volumes.items()})
                tasks.save(directory, task)
                try:
                    tasks.export_task(directory, task, box)
                except ValueError as error:
                    assert '--include-untracked' in str(error), error
                else:
                    raise AssertionError('Nested new files require explicit inclusion')
                first = tasks.export_task(directory, task, box, True)
                time.sleep(1.1)
                second = tasks.export_task(directory, task, box, True)
                assert first['audit'] == second['audit'], 'Repeated audits changed without file edits'
                report = fixture.root / 'checks.txt'
                report.write_text('Real Docker submodule transfer verified\n')
                second['report'] = str(report)
                tasks.save(directory, task)
                try:
                    tasks.cleanup(directory, task, box)
                except ValueError as error:
                    assert 'Ignored files' in str(error), error
                else:
                    raise AssertionError('Nested ignored files must block cleanup')
                with box.maintenance() as container:
                    dsh.run('docker', 'exec', container, 'python3', '-c',
                            f'from pathlib import Path; Path("/workspace/{fixture.path}/nested/ignored").unlink()')
                review = tasks.export_task(directory, task, box, True)
                review['report'] = str(report)
                tasks.save(directory, task)
                prepared = tasks.prepare(directory, task)
                work = Path(prepared['worktree'])
                assert (work / fixture.path / 'file').read_text() == 'base\n'
                assert not (work / fixture.path / 'nested/supplied-new').exists()
                assert (work / fixture.path / 'nested/agent-new').read_text() == 'agent work\n'
                (fixture.child / 'file').rename(fixture.root / 'preserved-baseline')
                git(fixture.child, 'checkout', '--', 'file')
                (fixture.leaf / 'supplied-new').rename(fixture.root / 'preserved-new')
                tasks.finalize(directory, task, report)
                assert (fixture.leaf / 'agent-new').read_text() == 'agent work\n'
                tasks.cleanup(directory, task, box, require_integrated=True)
                assert not work.exists()
                existing = tasks.docker_list('volume', 'ls', '--format', '{{.Name}}')
                assert volumes['workspace'] not in existing
                assert volumes['home'] in existing
                assert all(Path(item['bundle']).exists() for item in task['reviews'])
                print('PASS: recursive Docker init/audit, snapshots, repeated exports, nested file guards, '
                      'agent-only integration, offline finalize, cleanup and home retention.')
            finally:
                # Only the uniquely named fixture resources, including on failure.
                box.compose('down')
                existing = tasks.docker_list('volume', 'ls', '--format', '{{.Name}}')
                for volume in volumes.values():
                    if volume in existing:
                        dsh.run('docker', 'volume', 'rm', volume)
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
