#!/usr/bin/env python3
"""Explicit sandbox profiles and a Git bundle bridge. Python standard library only."""
import argparse
import copy
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


def plugin_paths(values, *, absolute=False, strict=False):
    if not isinstance(values, list):
        raise ValueError('Profile plugins must be an ordered list of paths.')
    paths = []
    for index, value in enumerate(values, 1):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Plugin {index} must be a nonempty string path.')
        path = Path(value).expanduser()
        if absolute and not path.is_absolute():
            raise ValueError(f'Plugin {index} must be an absolute path (or start with ~).')
        try:
            path = path.resolve(strict=strict)
        except OSError as error:
            raise ValueError(f'Plugin {index} directory is unavailable: {value}') from error
        if strict and not path.is_dir():
            raise ValueError(f'Plugin {index} is not a directory: {path}')
        if path in paths:
            raise ValueError(f'Duplicate plugin path at positions {paths.index(path) + 1} and {index}: {path}')
        paths.append(path)
    return [str(path) for path in paths]


class Plugin:
    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.is_relative_to(ROOT) and not self.directory.is_relative_to(ROOT / 'examples'):
            raise ValueError(f'Private plugins must live outside the public repository: {directory}')
        try:
            self.manifest = json.loads((self.directory / 'plugin.json').read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as error:
            raise ValueError(f'Cannot read plugin.json for {directory}') from error
        if (not isinstance(self.manifest, dict)
                or set(self.manifest) - {'version', 'runtime', 'compose', 'build'}
                or type(self.manifest.get('version')) is not int or self.manifest['version'] != 1):
            raise ValueError(f'Unsupported plugin.json contract: {directory}')
        for key in ('runtime', 'compose'):
            if key in self.manifest:
                path = self.path(self.manifest[key])
                if (key == 'runtime' and not path.is_dir()) or (key == 'compose' and not path.is_file()):
                    raise ValueError(f'Plugin {directory}: {key} must be a {"directory" if key == "runtime" else "file"}.')
        if 'build' in self.manifest:
            build = self.manifest['build']
            if not isinstance(build, dict) or set(build) - {'context', 'dockerfile'}:
                raise ValueError(f'Plugin {directory}: build allows only context and dockerfile.')
            if not self.path(build.get('context', '.')).is_dir() or not self.path(build.get('dockerfile', 'Dockerfile')).is_file():
                raise ValueError(f'Plugin {directory}: build needs a context directory and Dockerfile.')

    def path(self, relative):
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError(f'Plugin {self.directory}: manifest paths must be nonempty strings.')
        try:
            path = (self.directory / relative).resolve(strict=True)
        except OSError as error:
            raise ValueError(f'Plugin {self.directory}: missing manifest path {relative}') from error
        if not path.is_relative_to(self.directory):
            raise ValueError(f'Plugin {self.directory}: manifest paths must stay inside the selected plugin directory.')
        return path

    def extends_base(self):
        """Prove the final Dockerfile stage descends from the supplied image."""
        source = self.path(self.manifest['build'].get('dockerfile', 'Dockerfile')).read_text(encoding='utf-8-sig')
        source = re.sub(r'\\\r?\n', ' ', source)
        stages, declared, seen_from, inherited, stage_index = {}, False, False, False, 0
        for line in source.splitlines():
            line = line.strip()
            if not seen_from and re.match(r'ARG\s+DSH_BASE_IMAGE(?:\s|=|$)', line, re.I):
                declared = True
            match = re.fullmatch(r'FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?\s*', line, re.I)
            if match:
                seen_from = True
                base, alias = match.groups()
                inherited = (declared and base in ('${DSH_BASE_IMAGE}', '$DSH_BASE_IMAGE')) or stages.get(base.lower(), False)
                stages[str(stage_index)] = inherited
                stage_index += 1
                if alias:
                    stages[alias.lower()] = inherited
        return seen_from and inherited


def compose_plugins(core, contributions):
    """Compose only individually validated overrides; explicit equal values may agree."""
    result = copy.deepcopy(core)
    claims = {}

    def assign(path, value, plugin):
        if path in claims:
            owner, previous = claims[path]
            if previous != value:
                raise ValueError(f'Plugins {owner!s} and {plugin!s} conflict on Compose {".".join(path)}.')
        else:
            claims[path] = (plugin, value)
        target = result
        for key in path[:-1]:
            target = target.setdefault(key, {})
        if value is MISSING:
            target.pop(path[-1], None)
        else:
            target[path[-1]] = copy.deepcopy(value)

    for plugin, raw, config in contributions:
        validate_compose(config, core)
        supplied = raw.get('services', {}).get('dsh', {})
        service = config['services']['dsh']
        fixed = core['services']['dsh']
        environment = service.get('environment', {})
        baseline = fixed.get('environment', {})
        keys = set(supplied.get('environment') or {}) | {
            key for key in environment.keys() | baseline.keys()
            if environment.get(key, MISSING) != baseline.get(key, MISSING)}
        for key in sorted(keys):
            assign(('services', 'dsh', 'environment', key), environment.get(key, MISSING), plugin)
        for key in ('cpus', 'mem_limit', 'mem_reservation', 'pids_limit', 'shm_size', 'tmpfs'):
            if key in supplied or service.get(key, MISSING) != fixed.get(key, MISSING):
                value = service.get(key, MISSING)
                if key == 'tmpfs':
                    value = sorted(value)
                assign(('services', 'dsh', key), value, plugin)
        supplied_volumes = raw.get('volumes') or {}
        # Standalone Compose rendering can prune definitions only the core mounts use.
        keys = supplied_volumes.keys() | {
            key for key in config['volumes'] if config['volumes'][key] != core['volumes'][key]}
        for key in sorted(keys):
            if supplied_volumes.get(key) or config['volumes'][key] != core['volumes'][key]:
                assign(('volumes', key), config['volumes'][key], plugin)
    if result['volumes']['home']['name'] == result['volumes']['workspace']['name']:
        owners = [claims[('volumes', key)][0] for key in ('home', 'workspace')]
        raise ValueError(f'Plugins {owners[0]} and {owners[1]} conflict on Compose volumes: '
                         'home and workspace must use separate volumes.')
    validate_compose(result, core)
    return result


MISSING = object()


def compose_literals(value):
    """Keep resolved strings literal when Compose reads generated configuration."""
    if isinstance(value, str):
        return value.replace('$', '$$')
    if isinstance(value, list):
        return [compose_literals(item) for item in value]
    if isinstance(value, dict):
        return {key: compose_literals(item) for key, item in value.items()}
    return value


class Sandbox:
    def __init__(self, args, temp):
        self.args = args
        self.env = os.environ.copy()
        # Do not load a repository .env or implicit compose overrides.
        self.env.update(DSH_PORT=str(args.port), DSH_IMAGE='dsh-sandbox:local')
        self.plugins = [Plugin(path) for path in plugin_paths(args.plugins, strict=True)]
        builders = [plugin for plugin in self.plugins if 'build' in plugin.manifest]
        if len(self.plugins) > 1:
            for index, plugin in enumerate(builders):
                if not plugin.extends_base():
                    other = builders[index - 1] if index else next(p for p in self.plugins if p is not plugin)
                    raise ValueError(f'Plugins {other.directory} and {plugin.directory} have incompatible build contributions: '
                                     f'{plugin.directory} final stage must inherit ARG DSH_BASE_IMAGE / FROM ${{DSH_BASE_IMAGE}}.')
        self.builds = []
        for plugin in builders:
            prefix = self.plugins[:self.plugins.index(plugin) + 1]
            # Keep the established single-plugin image name; hash ordered prefixes for stacks.
            identity = str(plugin.directory) if len(self.plugins) == 1 else json.dumps([str(p.directory) for p in prefix])
            digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
            self.env['DSH_IMAGE'] = f'dsh-plugin:{digest}'
            self.builds.append((plugin, self.env['DSH_IMAGE']))
        extra = {'services': {'dsh': {}}}
        service = extra['services']['dsh']
        runtimes = []
        for index, plugin in enumerate(self.plugins):
            if 'runtime' in plugin.manifest:
                target = '/opt/dsh-plugin' if index == 0 else f'/opt/dsh-plugins/{index + 1}'
                service.setdefault('volumes', []).append(dict(type='bind',
                    source=str(plugin.path(plugin.manifest['runtime'])), target=target,
                    read_only=True, bind=dict(create_host_path=False)))
                runtimes.append(dict(plugin=str(plugin.directory), runtime=target))
        service['environment'] = dict(DSH_PLUGIN_RUNTIMES=json.dumps(runtimes))
        if args.key_file:
            key = external_file(args.key_file)
            service.setdefault('volumes', []).append(dict(type='bind', source=str(key),
                target='/run/secrets/deepseek', read_only=True, bind=dict(create_host_path=False)))
            service.setdefault('environment', {}).update(DEEPSEEK_API_KEY='', DEEPSEEK_API_KEY_FILE='/run/secrets/deepseek')
        generated = Path(temp) / 'profile.json'
        generated.write_text(json.dumps(compose_literals(extra)), encoding='utf-8')
        self.base_command = ['docker', 'compose', '--project-name', args.name,
                        '--project-directory', str(ROOT), '--env-file', os.devnull]
        self.core_command = self.base_command + ['-f', str(ROOT / 'compose.yaml'), '-f', str(generated)]
        core = json.loads(run(*self.core_command, 'config', '--format', 'json', capture=True, env=self.env))
        contributions = []
        for plugin in self.plugins:
            if 'compose' not in plugin.manifest:
                continue
            override = str(plugin.path(plugin.manifest['compose']))
            env = {**self.env, 'DSH_PLUGIN_DIR': str(plugin.directory)}
            try:
                raw = json.loads(run(*self.base_command, '-f', override, 'config', '--no-consistency',
                                     '--no-normalize', '--format', 'json', capture=True, env=env))
                config = json.loads(run(*self.base_command, '-f', str(ROOT / 'compose.yaml'), '-f', override,
                                       '-f', str(generated), 'config', '--format', 'json', capture=True, env=env))
                validate_compose(config, core)
            except (ValueError, subprocess.CalledProcessError) as error:
                raise ValueError(f'Plugin {plugin.directory}: {error}') from error
            contributions.append((plugin.directory, raw, config))
        composed = Path(temp) / 'composed.json'
        # `compose config` already escapes literal dollars for round-tripping.
        config = compose_plugins(core, contributions)
        composed.write_text(json.dumps(config), encoding='utf-8')
        self.command = self.base_command + ['-f', str(composed)]
        self.check_config()
        # Parsing needs only runtime files. Do not create task resources on a failed check.
        check = copy.deepcopy(config)
        check.pop('volumes', None)
        check.pop('networks', None)
        service = check['services']['dsh']
        service.pop('ports', None)
        service.pop('networks', None)
        service['network_mode'] = 'none'
        targets = {item['runtime'] for item in runtimes}
        service['volumes'] = [mount for mount in service.get('volumes', []) if mount['target'] in targets]
        service.setdefault('environment', {})['DEEPSEEK_API_KEY'] = ''
        service['environment'].pop('DEEPSEEK_API_KEY_FILE', None)
        preflight = Path(temp) / 'preflight.json'
        preflight.write_text(json.dumps(check), encoding='utf-8')
        self.preflight_command = self.base_command + ['-f', str(preflight)]

    def compose(self, *args, capture=False):
        return run(*self.command, *args, capture=capture, env=self.env)

    def check_config(self):
        config = json.loads(self.compose('config', '--format', 'json', capture=True))
        core = json.loads(run(*self.core_command, 'config', '--format', 'json',
                              capture=True, env=self.env))
        validate_compose(config, core)

    def build(self):
        run('docker', 'build', '-t', 'dsh-sandbox:local', ROOT)
        base = 'dsh-sandbox:local'
        for plugin, image in self.builds:
            build = plugin.manifest['build']
            run('docker', 'build', '-t', image, '--build-arg', 'DSH_BASE_IMAGE=' + base,
                '-f', plugin.path(build.get('dockerfile', 'Dockerfile')),
                plugin.path(build.get('context', '.')))
            base = image

    def preflight(self):
        if self.plugins:
            run(*self.preflight_command, 'run', '--rm', '--no-deps', '-T', '--entrypoint', 'node',
                'dsh', '/opt/dsh/plugin-runtime.mjs', 'check', env=self.env)

    @contextmanager
    def maintenance(self, *, allow_running=False):
        if not allow_running and self.compose('ps', '--status', 'running', '-q', capture=True):
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
        with sandbox.maintenance(allow_running=args.action == 'export' and
                                 getattr(args, 'force', False)) as container:
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


def profiles_path():
    if sys.platform == 'win32':
        base = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        if not base.is_absolute():
            base = Path.home() / '.config'
    return base / 'containerize-dsh' / 'profiles.json'


def load_profile(name):
    path = profiles_path().resolve()
    if path.is_relative_to(ROOT):
        raise ValueError('Keep the profile registry outside the public repository.')
    try:
        profiles = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise ValueError(f'Cannot read profile registry as JSON: {path}') from None
    if not isinstance(profiles, dict):
        raise ValueError('Profile registry must be an object keyed by profile name.')
    if name not in profiles:
        raise ValueError(f'Unknown profile: {name}')
    profile = profiles[name]
    if not isinstance(profile, dict) or set(profile) - {'plugin', 'plugins', 'repo', 'name', 'port', 'key_file'}:
        raise ValueError('Profile allows only plugin, plugins, repo, name, port and key_file.')
    if 'plugin' in profile and 'plugins' in profile:
        raise ValueError('Profile plugin and plugins are mutually exclusive.')
    profile = profile.copy()
    if 'plugin' in profile:
        profile['plugins'] = [profile.pop('plugin')]
    for key, value in profile.items():
        if key == 'plugins':
            profile[key] = plugin_paths(value, absolute=True)
        elif key == 'port':
            if type(value) is not int or not 1 <= value <= 65535:
                raise ValueError('Profile port must be an integer from 1 to 65535.')
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f'Profile {key} must be a nonempty string.')
        elif key in {'repo', 'key_file'}:
            expanded = Path(value).expanduser()
            if not expanded.is_absolute():
                raise ValueError(f'Profile {key} must be an absolute path (or start with ~).')
            profile[key] = str(expanded)
    return profile


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', help='Explicit user profile name from profiles.json')
    p.add_argument('--name', help='Compose project / persistent volume namespace')
    p.add_argument('--plugin', help='Explicit plugin directory, including external paths')
    p.add_argument('--key-file', help='External file containing only the DeepSeek API key')
    p.add_argument('--port', type=int)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ['build', 'up', 'stop', 'restart', 'down', 'logs', 'url', 'shell', 'validate', 'config']:
        sub.add_parser(action)
    for action in ['init', 'refresh', 'export', 'import', 'status']:
        child = sub.add_parser(action)
        if action in ('init', 'refresh', 'import', 'status'):
            child.add_argument('--repo')
        if action in ('init', 'refresh'):
            child.add_argument('--revision', default='HEAD')
        if action in ('init', 'refresh', 'export'):
            child.add_argument('--snapshot', action='store_true')
            child.add_argument('--include-untracked', action='store_true')
        if action in ('export', 'import'):
            child.add_argument('--bundle', required=True)
        if action == 'export':
            child.add_argument('--force', action='store_true',
                               help='Export while the sandbox is running; concurrent edits may produce an inconsistent snapshot')
        if action == 'import':
            child.add_argument('--branch')
    args = p.parse_args(argv)
    selected_plugin = vars(args).pop('plugin')
    args.plugins = [selected_plugin] if selected_plugin is not None else None
    if args.profile is not None:
        try:
            profile = load_profile(args.profile)
        except ValueError as error:
            p.error(str(error))
        for key, value in profile.items():
            if hasattr(args, key) and getattr(args, key) is None:
                setattr(args, key, value)
    if args.name is None:
        args.name = 'dsh-sandbox'
    if args.port is None:
        args.port = 11111
    if args.plugins is None:
        args.plugins = []
    if not re.fullmatch('[a-z0-9][a-z0-9_-]*', args.name):
        p.error('Invalid sandbox name')
    if not 1 <= args.port <= 65535:
        p.error('Port must be from 1 to 65535')
    if args.action in ('init', 'refresh', 'import') and not args.repo:
        p.error('--repo is required unless supplied by the selected profile')
    if getattr(args, 'include_untracked', False) and not args.snapshot:
        p.error('--include-untracked requires --snapshot')
    return args


def main():
    args = parse_args()
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
            sandbox.preflight()
            sandbox.compose('up', '-d', '--no-build')
        elif args.action == 'restart':
            sandbox.preflight()
            sandbox.compose('restart')
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
                'dsh --version; node /opt/dsh/plugin-runtime.mjs validate')
        elif args.action == 'config':
            print('Compose configuration passed isolation checks. No secrets printed.')
        else:
            sandbox.compose(args.action)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
