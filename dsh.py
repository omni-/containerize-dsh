#!/usr/bin/env python3
"""Explicit sandbox profiles and a Git bundle bridge. Python standard library only."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
from git_transfer import git, commit, make_bundle, bundle_refs


def run(*args, capture=False, env=None):
    result = subprocess.run(list(map(str, args)), env=env, check=True,
                            stdout=subprocess.PIPE if capture else None, text=True)
    return result.stdout.strip() if capture else None


def external_file(value):
    path = Path(value).resolve(strict=True)
    if path.is_relative_to(ROOT):
        raise ValueError('Keep secrets outside the public repository.')
    if not path.is_file():
        raise ValueError('Expected a file.')
    return path


def validate_compose(config, core):
    # Fail closed for new Compose features, including top-level network changes.
    if {k: v for k, v in config.items() if k not in ('services', 'volumes')} != {
            k: v for k, v in core.items() if k not in ('services', 'volumes')}:
        raise ValueError('Plugin override changes unsupported Compose settings.')
    if set(config['services']) != {'dsh'}:
        raise ValueError('This thin contract supports only the dsh service.')
    service = config['services']['dsh']
    fixed = core['services']['dsh']
    resources = {'cpus', 'mem_limit', 'mem_reservation', 'pids_limit', 'shm_size'}
    flexible = resources | {'environment', 'tmpfs'}
    if {k: v for k, v in service.items() if k not in flexible} != {
            k: v for k, v in fixed.items() if k not in flexible}:
        raise ValueError('Plugin override changes core isolation/startup settings.')
    if not {'pids_limit', 'shm_size'} <= service.keys():
        raise ValueError('Core PID and shared-memory limits cannot be removed.')
    for key in resources & service.keys():
        try:
            value = float(service[key]) if key == 'cpus' else int(service[key])
            if not 0 < value < float('inf'):
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError(f'{key} must be a finite positive resource limit.') from None
    # Only size and exec/noexec can vary; never overlay system paths or change ownership.
    temporary = service.get('tmpfs', [])
    targets = []
    for mount in temporary:
        match = re.fullmatch(
            r'(/tmp|/var/tmp):size=[1-9][0-9]*[kKmMgG]?,(?:exec|noexec),mode=1777,nosuid,nodev', mount)
        if not match:
            raise ValueError('tmpfs permits only core temporary paths, sizes and exec/noexec.')
        targets.append(match[1])
    if sorted(targets) != ['/tmp', '/var/tmp']:
        raise ValueError('Both core tmpfs targets must appear exactly once.')
    volumes = config.get('volumes', {})
    if set(volumes) != set(core['volumes']):
        raise ValueError('Only the core home and workspace volumes are supported.')
    for volume in volumes.values():
        if (set(volume) - {'name', 'external', 'driver'}
                or volume.get('driver', 'local') != 'local'):
            raise ValueError('Only local named volumes, optionally external, are supported.')
    if len({v['name'] for v in volumes.values()}) != len(volumes):
        raise ValueError('Home and workspace must use separate volumes.')


class Sandbox:
    def __init__(self, args, temp):
        self.args = args
        self.env = os.environ.copy()
        # Do not load a repository .env or implicit compose overrides.
        self.env.update(DSH_PORT=str(args.port), DSH_IMAGE='dsh-sandbox:local')
        self.plugin = None
        self.manifest = {}
        files = [ROOT / 'compose.yaml']
        extra = {'services': {'dsh': {}}}
        service = extra['services']['dsh']
        if args.plugin:
            self.plugin = Path(args.plugin).resolve(strict=True)
            if self.plugin.is_relative_to(ROOT) and not self.plugin.is_relative_to(ROOT / 'examples'):
                raise ValueError('Private plugins must live outside the public repository.')
            self.manifest = json.loads((self.plugin / 'plugin.json').read_text())
            if set(self.manifest) - {'version', 'runtime', 'compose', 'build'} or self.manifest.get('version') != 1:
                raise ValueError('Unsupported plugin.json contract')
            self.env['DSH_PLUGIN_DIR'] = str(self.plugin)
            if 'compose' in self.manifest:
                files.append(self.path(self.manifest['compose']))
            if 'runtime' in self.manifest:
                runtime = self.path(self.manifest['runtime'])
                service['volumes'] = [dict(type='bind', source=str(runtime),
                    target='/opt/dsh-plugin', read_only=True,
                    bind=dict(create_host_path=False))]
            if 'build' in self.manifest:
                digest = hashlib.sha256(str(self.plugin).encode()).hexdigest()[:12]
                self.env['DSH_IMAGE'] = f'dsh-plugin:{digest}'
        if args.key_file:
            key = external_file(args.key_file)
            service.setdefault('volumes', []).append(dict(type='bind', source=str(key),
                target='/run/secrets/deepseek', read_only=True, bind=dict(create_host_path=False)))
            service['environment'] = dict(DEEPSEEK_API_KEY='', DEEPSEEK_API_KEY_FILE='/run/secrets/deepseek')
        generated = Path(temp) / 'profile.json'
        generated.write_text(json.dumps(extra), encoding='utf-8')
        files.append(generated)
        self.command = ['docker', 'compose', '--project-name', args.name,
                        '--project-directory', str(ROOT), '--env-file', os.devnull]
        self.core_command = self.command + ['-f', str(ROOT / 'compose.yaml'), '-f', str(generated)]
        for file in files:
            self.command.extend(['-f', str(file)])
        self.check_config()

    def path(self, relative):
        path = (self.plugin / relative).resolve(strict=True)
        if not path.is_relative_to(self.plugin):
            raise ValueError('Manifest paths must stay inside the selected plugin directory.')
        return path

    def compose(self, *args, capture=False):
        return run(*self.command, *args, capture=capture, env=self.env)

    def check_config(self):
        config = json.loads(self.compose('config', '--format', 'json', capture=True))
        core = json.loads(run(*self.core_command, 'config', '--format', 'json',
                              capture=True, env=self.env))
        validate_compose(config, core)

    def build(self):
        run('docker', 'build', '-t', 'dsh-sandbox:local', ROOT)
        if 'build' in self.manifest:
            build = self.manifest['build']
            run('docker', 'build', '-t', self.env['DSH_IMAGE'],
                '--build-arg', 'DSH_BASE_IMAGE=dsh-sandbox:local',
                '-f', self.path(build.get('dockerfile', 'Dockerfile')),
                self.path(build.get('context', '.')))

    @contextmanager
    def maintenance(self):
        if self.compose('ps', '--status', 'running', '-q', capture=True):
            raise ValueError('Stop the sandbox before workspace operations: dsh.py ... stop')
        name = 'dsh-transfer-' + uuid.uuid4().hex
        try:
            self.compose('run', '-d', '--no-deps', '--name', name,
                         '--entrypoint', 'sleep', 'dsh', 'infinity', capture=True)
            yield name
        finally:
            run('docker', 'rm', '-f', name, capture=True)


def transfer(sandbox, args):
    if args.action == 'import':
        repo = Path(args.repo).resolve(strict=True)
        bundle = Path(args.bundle).resolve(strict=True)
        branch = args.branch or 'dsh/review-' + uuid.uuid4().hex[:12]
        if not branch.startswith('dsh/review-'):
            raise ValueError('Review branches must start with dsh/review-')
        git(repo, 'check-ref-format', '--branch', branch)
        existing = git(repo, 'for-each-ref', f'refs/heads/{branch}', '--format=%(refname)')
        if existing:
            raise ValueError('Review branch already exists; choose a new name.')
        refs = bundle_refs(repo, bundle, {'work', 'base', 'host'})
        # Check before fetch: the bundle itself must not supply the destination's identity.
        try:
            kind = git(repo, 'cat-file', '-t', refs['host'][0],
                       env={**os.environ, 'GIT_NO_LAZY_FETCH': '1'})
        except RuntimeError:
            kind = None
        if kind != 'commit':
            raise ValueError('Bundle host commit is not already present in the destination repository.')
        # Fetch only objects first, then atomically create a new branch (never overwrite).
        git(repo, '-c', 'core.hooksPath=' + os.devnull, 'fetch', '--no-tags', '--no-write-fetch-head',
            bundle, *[value[1] for value in refs.values()])
        git(repo, 'merge-base', '--is-ancestor', refs['base'][0], refs['work'][0])
        git(repo, 'update-ref', f'refs/heads/{branch}', refs['work'][0], '0' * len(refs['work'][0]))
        print(f'Review branch: {branch}\nHost source: {refs["host"][0]}\nDSH base: {refs["base"][0]}')
        print(f'Agent-only diff: git diff {refs["base"][0]} {branch}')
        return
    with tempfile.TemporaryDirectory(prefix='dsh-bundle-') as temp:
        bundle = Path(temp) / 'transfer.bundle'
        if args.action in ('init', 'refresh'):
            repo = Path(args.repo).resolve(strict=True)
            # Always operate from the root, even if the caller gives a subdirectory.
            repo = Path(git(repo, 'rev-parse', '--show-toplevel'))
            base, head = commit(repo, args.revision, args.snapshot, args.include_untracked)
            make_bundle(repo, bundle, dict(base=base, host=head))
        if args.action == 'export' and Path(args.bundle).exists():
            raise ValueError('Export destination already exists.')
        with sandbox.maintenance() as container:
            command = ['docker', 'exec', container, 'python3', '/opt/dsh/workspace.py', args.action]
            if args.action in ('init', 'refresh'):
                with bundle.open('rb') as stream:
                    subprocess.run(['docker', 'exec', '-i', container, 'python3', '-c',
                        'import sys; open("/tmp/input.bundle", "wb").write(sys.stdin.buffer.read())'],
                        stdin=stream, check=True)
                command += ['--bundle', '/tmp/input.bundle']
            elif args.action == 'export':
                command += ['--bundle', '/tmp/output.bundle']
                if args.snapshot:
                    command += ['--snapshot']
                if args.include_untracked:
                    command += ['--include-untracked']
            output = run(*command, capture=args.action == 'status')
            if args.action == 'export':
                with Path(args.bundle).resolve().open('xb') as stream:
                    subprocess.run(['docker', 'exec', container, 'cat', '/tmp/output.bundle'],
                                   stdout=stream, check=True)
            if args.action == 'status':
                state = json.loads(output)
                print(json.dumps(state, indent=2))
                if args.repo:
                    host = git(args.repo, 'rev-parse', 'HEAD')
                    print('Current host HEAD:', host)
                    print('Host changed since transfer:', host != state['host'])
                    print('Host changes:', git(args.repo, 'status', '--short') or '(clean)')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--name', default='dsh-sandbox', help='Compose project / persistent volume namespace')
    p.add_argument('--plugin', help='Explicit plugin directory, including external paths')
    p.add_argument('--key-file', help='External file containing only the DeepSeek API key')
    p.add_argument('--port', type=int, default=11111)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ['build', 'up', 'stop', 'restart', 'down', 'logs', 'url', 'shell', 'validate', 'config']:
        sub.add_parser(action)
    for action in ['init', 'refresh', 'export', 'import', 'status']:
        child = sub.add_parser(action)
        if action in ('init', 'refresh', 'import', 'status'):
            child.add_argument('--repo', required=action != 'status')
        if action in ('init', 'refresh'):
            child.add_argument('--revision', default='HEAD')
        if action in ('init', 'refresh', 'export'):
            child.add_argument('--snapshot', action='store_true')
            child.add_argument('--include-untracked', action='store_true')
        if action in ('export', 'import'):
            child.add_argument('--bundle', required=True)
        if action == 'import':
            child.add_argument('--branch')
    args = p.parse_args()
    if not re.fullmatch('[a-z0-9][a-z0-9_-]*', args.name):
        p.error('Invalid sandbox name')
    if getattr(args, 'include_untracked', False) and not args.snapshot:
        p.error('--include-untracked requires --snapshot')
    if args.action == 'import':
        transfer(None, args)
        return
    with tempfile.TemporaryDirectory(prefix='dsh-compose-') as temp:
        sandbox = Sandbox(args, temp)
        if args.action in ('init', 'refresh', 'export', 'status'):
            transfer(sandbox, args)
        elif args.action == 'build':
            sandbox.build()
        elif args.action == 'up':
            sandbox.compose('up', '-d', '--no-build')
        elif args.action == 'url':
            logs = sandbox.compose('logs', '--no-color', '--tail', '200', 'dsh', capture=True)
            tokens = re.findall(r'http://127\.0\.0\.1:3080/\?token=([^\s]+)', logs)
            if not tokens:
                raise ValueError('No browser login URL yet. Check logs and retry after startup.')
            print(f'http://127.0.0.1:{args.port}/?token={tokens[-1]}')
        elif args.action == 'shell':
            sandbox.compose('exec', 'dsh', 'bash')
        elif args.action == 'validate':
            sandbox.compose('exec', '-T', 'dsh', 'sh', '-eu', '-c',
                'test "$(id -u)" = 10001; test -w /workspace; test -w /home/agent; '
                'dsh --version; if [ -f /opt/dsh-plugin/validate.sh ]; then '
                'sh /opt/dsh-plugin/validate.sh; fi')
        elif args.action == 'config':
            print('Compose configuration passed isolation checks. No secrets printed.')
        else:
            sandbox.compose(args.action)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
