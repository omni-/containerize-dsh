"""Explicit local integration test. Uses only synthetic data and fresh named volumes."""
import json
import http.cookiejar
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from git_transfer import git


def run(*args, ok=True):
    result = subprocess.run(list(map(str, args)), capture_output=True, text=True)
    if ok and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    if not ok and not result.returncode:
        raise AssertionError('Expected command to be rejected')
    return result.stdout.strip()


def check_compose_overrides(cli, plugin):
    manifest = plugin / 'plugin.json'
    original = manifest.read_text()
    profile = json.loads(original)
    profile['compose'] = 'hardening.yaml'
    override = plugin / 'hardening.yaml'
    manifest.write_text(json.dumps(profile))
    try:
        for settings, message in [
            ({'security_opt': ['seccomp:unconfined']}, 'core isolation/startup'),
            ({'security_opt': ['apparmor:unconfined']}, 'core isolation/startup'),
            ({'tmpfs': ['/etc:mode=1777']}, 'tmpfs permits only'),
            ({'userns_mode': 'host'}, 'core isolation/startup'),
        ]:
            override.write_text(json.dumps({'services': {'dsh': settings}}))
            result = subprocess.run([*map(str, cli), 'config'], capture_output=True, text=True)
            assert result.returncode != 0 and message in result.stderr, result.stderr
        override.write_text('services:\n  dsh:\n    pids_limit: !reset null\n', newline='\n')
        result = subprocess.run([*map(str, cli), 'config'], capture_output=True, text=True)
        assert result.returncode != 0 and 'limit' in result.stderr, result.stderr
        override.write_text('''services:
  dsh:
    environment:
      EXAMPLE_GREETING: hello
    cpus: 2
    mem_limit: 512m
    pids_limit: 4096
    tmpfs: !override
      - /tmp:size=4g,exec,mode=1777,nosuid,nodev
      - /var/tmp:size=512m,noexec,mode=1777,nosuid,nodev
volumes:
  home:
    external: true
    name: existing-home
''', newline='\n')
        run(*cli, 'config')
    finally:
        manifest.write_text(original)


def main():
    name = 'dsh-test-' + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix='dsh-smoke-') as temp:
        directory = Path(temp)
        plugin = directory / 'external plugin'
        shutil.copytree(ROOT / 'examples' / 'hello', plugin)
        cli = [sys.executable, ROOT / 'dsh.py', '--name', name,
               '--plugin', plugin, '--port', '11112']
        repo = directory / 'host checkout'
        repo.mkdir()
        git(repo, 'init')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@localhost')
        (repo / 'file.txt').write_text('base\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        (repo / 'file.txt').write_text('staged\n')
        git(repo, 'add', '.')
        (repo / 'file.txt').write_text('snapshot\n')
        index = (repo / '.git/index').read_bytes()
        head = git(repo, 'rev-parse', 'HEAD')
        try:
            check_compose_overrides(cli, plugin)
            run(*cli, 'build')
            run(*cli, 'init', '--repo', repo, '--snapshot')
            assert index == (repo / '.git/index').read_bytes()
            assert git(repo, 'rev-parse', 'HEAD') == head
            run(*cli, 'up')
            cid = run('docker', 'ps', '-q', '--filter', f'label=com.docker.compose.project={name}')
            browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            for attempt in range(60):
                try:
                    url = run(*cli, 'url')
                    with browser.open(url, timeout=2) as response:
                        assert response.status == 200
                    break
                except Exception:
                    time.sleep(1)
            else:
                raise AssertionError('DSH web did not become ready')
            run(*cli, 'validate')
            run('docker', 'exec', cid, 'sh', '-ec',
                '. /etc/os-release; test "$ID" = ubuntu; test "$VERSION_ID" = 24.04; '
                'if command -v dotnet || command -v Xvfb; then exit 1; fi')
            info = json.loads(run('docker', 'inspect', cid))[0]
            assert info['HostConfig']['ReadonlyRootfs']
            assert info['Config']['User'] == '10001:10001'
            assert info['HostConfig']['CapDrop'] == ['ALL']
            assert all(p['HostIp'] == '127.0.0.1' for p in info['HostConfig']['PortBindings']['3081/tcp'])
            assert len(info['Mounts']) == 3
            run('docker', 'exec', cid, 'sh', '-c', 'test ! -e /var/run/docker.sock; test ! -d /home/agent/.ssh; test ! -e /home/agent/.git-credentials')
            run('docker', 'exec', cid, 'touch', '/forbidden', ok=False)
            run(*cli, 'refresh', '--repo', repo, ok=False)
            # Directory mounts see atomic config replacement after restart.
            replacement = plugin / 'runtime/new.sh'
            replacement.write_text('printf refreshed > "$HOME/reloaded"\n', newline='\n')
            replacement.replace(plugin / 'runtime/bootstrap.sh')
            run(*cli, 'restart')
            for attempt in range(30):
                result = subprocess.run(['docker', 'exec', cid, 'test', '-f', '/home/agent/reloaded'])
                if result.returncode == 0:
                    break
                time.sleep(1)
            assert run('docker', 'exec', cid, 'cat', '/home/agent/reloaded') == 'refreshed'
            run('docker', 'exec', cid, 'sh', '-c', 'printf "agent work\\n" >> /workspace/file.txt')
            run(*cli, 'stop')
            run(*cli, 'refresh', '--repo', repo, ok=False)
            bundle = directory / 'work.bundle'
            run(*cli, 'export', '--snapshot', '--bundle', bundle)
            report = run(*cli, 'import', '--repo', repo, '--bundle', bundle, '--branch', 'dsh/review-smoke')
            assert 'agent work' in git(repo, 'show', 'dsh/review-smoke:file.txt')
            assert git(repo, 'rev-parse', 'HEAD') == head
            assert (repo / '.git/index').read_bytes() == index
            assert (repo / 'file.txt').read_text() == 'snapshot\n'
            run(*cli, 'import', '--repo', repo, '--bundle', bundle, '--branch', 'dsh/review-smoke', ok=False)
            print(run(*cli, 'status', '--repo', repo))
            # Commit the exported dirty work, then refresh from a chosen revision.
            run(*cli, 'up')
            run('docker', 'exec', cid, 'git', 'add', 'file.txt')
            run('docker', 'exec', cid, 'git', 'commit', '-m', 'agent change')
            run(*cli, 'stop')
            run(*cli, 'refresh', '--repo', repo, '--revision', 'HEAD')
            print('PASS: external plugin, web, isolation, restart, snapshots, export/import, refresh.')
        finally:
            # Logs can contain browser tokens; do not print them in a successful run.
            run(*cli, 'down')
            run('docker', 'volume', 'rm', name + '_workspace', name + '_home')


if __name__ == '__main__':
    main()
