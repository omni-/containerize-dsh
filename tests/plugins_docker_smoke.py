"""Ordered saved-profile composition, using synthetic plugins and fresh volumes."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dsh
import dsh_task
from git_transfer import git


def plugin(root, name, index):
    directory = root / (name + ' plugin $literal')
    runtime = directory / 'runtime'
    runtime.mkdir(parents=True)
    target = '/opt/dsh-plugin' if index == 1 else f'/opt/dsh-plugins/{index}'
    (directory / 'plugin.json').write_text(json.dumps({
        'version': 1, 'runtime': 'runtime', 'compose': 'compose.json',
        'build': {'context': '.', 'dockerfile': 'Dockerfile'},
    }))
    # The second layer can only build if it actually inherited the first.
    inherited = 'RUN test -f /opt/one-image\n' if index == 2 else ''
    (directory / 'Dockerfile').write_text(
        'ARG DSH_BASE_IMAGE\nFROM ${DSH_BASE_IMAGE}\nUSER root\n' + inherited +
        f'RUN printf {name} > /opt/{name}-image\nUSER 10001:10001\n', newline='\n')
    (directory / 'compose.json').write_text(json.dumps({'services': {'dsh': {
        'environment': {name.upper(): '${DSH_PLUGIN_DIR}', 'SHARED': 'same',
                        'LITERAL': '$${DSH_COMPOSITION_SENTINEL}'},
        'cpus': 2, **({'mem_limit': '1g'} if index == 2 else {}),
    }}}))
    (runtime / 'data.txt').write_text(name)
    (runtime / 'bootstrap.sh').write_text(
        'set -eu\n' + f'test "$(cat "$DSH_PLUGIN_RUNTIME/data.txt")" = {name}\n'
        + ('test "$(cat "$HOME/bootstrap-order")" = one\n' if index == 2 else '')
        + f'printf "{name}\\n" {">" if index == 1 else ">>"} "$HOME/bootstrap-order"\n', newline='\n')
    (runtime / 'validate.sh').write_text(
        'set -eu\n' + f'test -f /opt/{name}-image\n'
        + f'test "$(cat "$DSH_PLUGIN_RUNTIME/data.txt")" = {name}\n'
        + ('test "$(cat "$HOME/validate-order")" = one\n' if index == 2 else '')
        + f'printf "{name}\\n" {">" if index == 1 else ">>"} "$HOME/validate-order"\n', newline='\n')
    (runtime / 'observer.mjs').write_text(
        "import { writeFileSync } from 'node:fs';\n"
        f"export const name = 'observer-{name}';\n"
        f"export function apply() {{ writeFileSync('/home/agent/cordis-{name}', '{name}'); }}\n", newline='\n')
    server = (ROOT / 'examples/hello/runtime/server.mjs').read_text()
    (runtime / 'server.mjs').write_text(
        "import { writeFileSync } from 'node:fs';\n"
        f"writeFileSync('/home/agent/mcp-{name}', '{name}');\n" + server, newline='\n')
    (runtime / 'cordis.yml').write_text(json.dumps([{'insert': [
        {'id': 'observer-' + name, 'name': './observer.mjs'},
        {'id': 'mcp-' + name, 'name': '@deepseek-ai/dsh-mcp-client', 'config': {
            'serverName': name, 'transport': 'stdio', 'command': 'node',
            'args': [target + '/server.mjs'], 'cwd': '/workspace', 'failOnStartupError': True,
        }},
    ]}]))
    return directory


def rejected(box, message, paths):
    # Capture stderr so any startup diagnostic is available without printing login tokens.
    result = subprocess.run([*box.preflight_command, 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'node',
                             'dsh', '/opt/dsh/plugin-runtime.mjs', 'check'],
                            env=box.env, capture_output=True, text=True)
    assert result.returncode, 'Expected incompatible patches to fail preflight'
    assert message in result.stderr, result.stderr
    assert all(str(path) in result.stderr for path in paths), result.stderr


def main():
    os.environ['DEEPSEEK_API_KEY'] = ''
    os.environ['DSH_COMPOSITION_SENTINEL'] = 'must-never-expand'
    with tempfile.TemporaryDirectory(prefix='dsh-plugins-smoke-') as temp:
        root = Path(temp)
        paths = [plugin(root, 'one', 1), plugin(root, 'two', 2)]
        registry = root / 'config/profiles.json'
        registry.parent.mkdir()
        repo = root / 'synthetic-repo'
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@localhost')
        (repo / 'file').write_text('base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        registry.write_text(json.dumps({'stack': {'plugins': list(map(str, paths)),
                                                 'repo': str(repo), 'port': port}}))
        task = None
        with patch.object(dsh, 'profiles_path', return_value=registry):
            try:
                output = io.StringIO()
                with redirect_stdout(output):
                    dsh_task.main(['start', '--task', 'composition', '--profile', 'stack'])
                manifests = list(dsh_task.state_root().glob('*/manifest.json'))
                assert len(manifests) == 1
                task = json.loads(manifests[0].read_text())
                assert task['resolved']['plugins'] == list(map(str, paths))
                assert f'http://127.0.0.1:{port}/?token=' in output.getvalue(), 'Web URL not ready'
                cid = task['container']
                assert dsh.run('docker', 'exec', cid, 'dsh', '--version', capture=True) == '0.2.1-alpha.2'
                info = json.loads(dsh.run('docker', 'inspect', cid, capture=True))[0]
                assert info['HostConfig']['ReadonlyRootfs'] and info['Config']['User'] == '10001:10001'
                assert info['HostConfig']['CapDrop'] == ['ALL']
                assert len(info['Mounts']) == 4
                assert all(m['RW'] is False for m in info['Mounts'] if m['Type'] == 'bind')
                environment = dict(v.split('=', 1) for v in info['Config']['Env'])
                assert environment['ONE'] == str(paths[0]) and environment['TWO'] == str(paths[1])
                assert environment['SHARED'] == 'same'
                assert environment['LITERAL'] == '${DSH_COMPOSITION_SENTINEL}'
                assert info['HostConfig']['NanoCpus'] == 2_000_000_000
                assert info['HostConfig']['Memory'] == 1_073_741_824
                assert dsh.run('docker', 'exec', cid, 'cat', '/home/agent/bootstrap-order', capture=True) == 'one\ntwo'
                for name in ('one', 'two'):
                    dsh.run('docker', 'exec', cid, 'sh', '-ec',
                            f'test -s /home/agent/cordis-{name}; test -s /home/agent/mcp-{name}')
                # Both repeatable patch arguments reach DSH in order and relative modules load.
                dump = dsh.run('docker', 'exec', cid, 'dsh', 'web', '--dump-config',
                               '--patch', '/opt/dsh-plugin/cordis.yml',
                               '--patch', '/opt/dsh-plugins/2/cordis.yml', capture=True)
                assert dump.index('id: observer-one') < dump.index('id: observer-two')
                with dsh_task.sandbox(task) as box:
                    box.compose('exec', '-T', 'dsh', 'node', '/opt/dsh/plugin-runtime.mjs', 'validate')
                    assert dsh.run('docker', 'exec', cid, 'cat', '/home/agent/validate-order', capture=True) == 'one\ntwo'
                    # Invalid patches fail before a bootstrap hook can run.
                    second_patch = paths[1] / 'runtime/cordis.yml'
                    original = second_patch.read_text()
                    for payload, message in (
                        ([{'insert': [{'id': 'observer-one', 'name': './observer.mjs'}]}], 'inserted id observer-one'),
                        ([{'id': 'mcp-one', 'config': {'serverName': 'different'}}], 'mcp-one.config'),
                        ([{'insert': [{'id': 'another-mcp', 'name': '@deepseek-ai/dsh-mcp-client',
                                      'config': {'serverName': 'one'}}]}], 'MCP serverName one'),
                    ):
                        second_patch.write_text(json.dumps(payload))
                        rejected(box, message, paths)
                    second_patch.write_text(original)
                    # Even a new namespace with conflicting Cordis must have no retained resources.
                    isolated = task['resolved'].copy()
                    isolated['name'] = task['resolved']['name'] + '-rejected'
                    second_patch.write_text(json.dumps([{'insert': [{'id': 'observer-one', 'name': './observer.mjs'}]}]))
                    with tempfile.TemporaryDirectory() as check_temp:
                        check_box = dsh.Sandbox(dsh_task.args_for({'resolved': isolated}, 'config'), check_temp)
                        rejected(check_box, 'inserted id observer-one', paths)
                        assert not dsh_task.docker_list('volume', 'ls', '--format', '{{.Name}}', '--filter',
                                                       'label=com.docker.compose.project=' + isolated['name'])
                        assert not dsh_task.docker_list('network', 'ls', '--format', '{{.Name}}', '--filter',
                                                       'label=com.docker.compose.project=' + isolated['name'])
                        assert not dsh_task.docker_list('ps', '-aq', '--filter',
                                                       'label=com.docker.compose.project=' + isolated['name'])
                    second_patch.write_text(original)
                    assert dsh.run('docker', 'exec', cid, 'cat', '/home/agent/bootstrap-order', capture=True) == 'one\ntwo'
                    # Hook failure must stop before the next plugin executes, in either mode.
                    for mode in ('bootstrap', 'validate'):
                        first_hook = paths[0] / f'runtime/{mode}.sh'
                        second_hook = paths[1] / f'runtime/{mode}.sh'
                        originals = (first_hook.read_text(), second_hook.read_text())
                        first_hook.write_text('exit 3\n', newline='\n')
                        second_hook.write_text(f'touch "$HOME/unexpected-{mode}"\n', newline='\n')
                        result = subprocess.run([*box.command, 'exec', '-T', 'dsh', 'node',
                                                 '/opt/dsh/plugin-runtime.mjs', mode],
                                                env=box.env, capture_output=True, text=True)
                        assert result.returncode and str(paths[0]) in result.stderr, result.stderr
                        dsh.run('docker', 'exec', cid, 'test', '!', '-e', '/home/agent/unexpected-' + mode)
                        first_hook.write_text(originals[0], newline='\n')
                        second_hook.write_text(originals[1], newline='\n')
                    # DSH's headless surface can also boot its CLI without a model request.
                    help_text = dsh.run('docker', 'exec', cid, 'dsh', 'headless', '--help', capture=True)
                    assert 'Usage:' in help_text
                    # The saved profile can disappear: later operations use the original record.
                    registry.write_text('{}')
                    box.compose('stop')
                with redirect_stdout(io.StringIO()):
                    dsh_task.main(['resume', task['id']])
                with dsh_task.sandbox(task) as box:
                    assert [str(p.directory) for p in box.plugins] == list(map(str, paths))
                    assert box.env['DSH_IMAGE'] == info['Config']['Image']
                    box.compose('stop')
                # Incompatible Compose and build contributions fail before Docker mutation.
                compose = paths[1] / 'compose.json'
                original = compose.read_text()
                data = json.loads(original)
                data['services']['dsh']['environment']['SHARED'] = 'different'
                compose.write_text(json.dumps(data))
                with tempfile.TemporaryDirectory() as config_temp:
                    try:
                        dsh.Sandbox(dsh_task.args_for(task, 'config'), config_temp)
                    except ValueError as error:
                        assert all(str(p) in str(error) for p in paths) and 'environment.SHARED' in str(error)
                    else:
                        raise AssertionError('Conflicting Compose values accepted')
                compose.write_text(original)
                dockerfile = paths[1] / 'Dockerfile'
                original = dockerfile.read_text()
                dockerfile.write_text('FROM dsh-sandbox:local\n', newline='\n')
                with tempfile.TemporaryDirectory() as config_temp:
                    try:
                        dsh.Sandbox(dsh_task.args_for(task, 'config'), config_temp)
                    except ValueError as error:
                        assert all(str(p) in str(error) for p in paths) and 'incompatible build' in str(error)
                    else:
                        raise AssertionError('Stack-resetting Dockerfile accepted')
                dockerfile.write_text(original, newline='\n')
                print('PASS: DSH 0.2.1-alpha.2 web/headless, ordered image/runtime/Cordis/MCP/hooks, Compose/build conflicts, isolation, and saved task stability.')
            finally:
                # Delete only resources in this unique fixture namespace, even after failed startup.
                if task is None:
                    records = list(dsh_task.state_root().glob('*/manifest.json'))
                    if records:
                        task = json.loads(records[0].read_text())
                if task:
                    name = task['resolved']['name']
                    for cid in dsh_task.docker_list('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + name):
                        dsh.run('docker', 'rm', '-f', cid, capture=True)
                    available = dsh_task.docker_list('volume', 'ls', '--format', '{{.Name}}')
                    for volume in task['volumes'].values():
                        if volume in available:
                            dsh.run('docker', 'volume', 'rm', volume, capture=True)
                    networks = dsh_task.docker_list('network', 'ls', '--format', '{{.Name}}')
                    if name + '_default' in networks:
                        dsh.run('docker', 'network', 'rm', name + '_default', capture=True)


if __name__ == '__main__':
    main()
