import copy
from contextlib import redirect_stdout
import io
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dsh
from git_transfer import git, make_bundle


class ImportBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.repository('source')
        self.host = git(self.source, 'rev-parse', 'HEAD')
        self.destination = self.root / 'destination'
        subprocess.run(['git', 'clone', '--no-local', str(self.source), str(self.destination)],
                       capture_output=True, check=True)
        self.configure(self.destination)
        (self.source / 'file').write_text('agent work')
        git(self.source, 'commit', '-am', 'agent work')
        self.work = git(self.source, 'rev-parse', 'HEAD')
        self.bundle = self.root / 'work.bundle'
        make_bundle(self.source, self.bundle, dict(host=self.host, base=self.host, work=self.work))

    def configure(self, repo):
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@localhost')

    def repository(self, name):
        repo = self.root / name
        repo.mkdir()
        git(repo, 'init')
        self.configure(repo)
        (repo / 'file').write_text(name)
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', name)
        return repo

    def import_into(self, repo):
        args = SimpleNamespace(action='import', repo=repo, bundle=self.bundle,
                               branch='dsh/review-boundary')
        with redirect_stdout(io.StringIO()):
            dsh.transfer(None, args)

    def test_unrelated_repository_rejected_before_fetch(self):
        repo = self.repository('unrelated')
        head = git(repo, 'rev-parse', 'HEAD')
        index = (repo / '.git/index').read_bytes()
        with patch.object(dsh, 'git', wraps=git) as calls:
            with self.assertRaisesRegex(ValueError, 'host commit.*not already present'):
                self.import_into(repo)
        self.assertFalse(any('fetch' in call.args[1:] for call in calls.call_args_list))
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual((repo / '.git/index').read_bytes(), index)
        self.assertEqual(git(repo, 'for-each-ref', 'refs/heads/dsh/review-boundary'), '')
        for oid in (self.host, self.work):
            with self.assertRaises(RuntimeError):
                git(repo, 'cat-file', '-t', oid)

    def test_advanced_dirty_linked_worktree_accepts_original_host(self):
        (self.destination / 'file').write_text('host advanced')
        git(self.destination, 'commit', '-am', 'host advanced')
        repo = self.root / 'linked checkout'
        git(self.destination, 'worktree', 'add', '-b', 'host-work', repo)
        (repo / 'file').write_text('staged')
        git(repo, 'add', 'file')
        (repo / 'file').write_text('unstaged')
        index = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        before = index.read_bytes()
        head = git(repo, 'rev-parse', 'HEAD')
        self.assertNotEqual(head, self.host)
        self.import_into(repo)
        self.assertEqual(git(repo, 'rev-parse', 'dsh/review-boundary'), self.work)
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual((repo / 'file').read_text(), 'unstaged')


class ComposeBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Shape of normalized `docker compose config --format json`, not raw YAML.
        self.core = {
            'name': 'test', 'networks': {'default': {'name': 'test_default', 'ipam': {}}},
            'volumes': {'home': {'name': 'test_home'}, 'workspace': {'name': 'test_workspace'}},
            'services': {'dsh': {
                'image': 'dsh-sandbox:local', 'user': '10001:10001', 'init': True,
                'working_dir': '/workspace', 'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'],
                'command': None, 'entrypoint': None, 'networks': {'default': None},
                'environment': {}, 'pids_limit': 2048, 'shm_size': '1073741824',
                'ports': [{'host_ip': '127.0.0.1', 'published': '11111', 'target': 3081}],
                'volumes': [
                    {'type': 'volume', 'source': 'home', 'target': '/home/agent', 'volume': {}},
                    {'type': 'volume', 'source': 'workspace', 'target': '/workspace', 'volume': {}},
                ],
                'tmpfs': ['/tmp:size=2g,exec,mode=1777,nosuid,nodev',
                          '/var/tmp:size=1g,exec,mode=1777,nosuid,nodev'],
            }},
        }

    def test_documented_overrides_accepted(self):
        config = copy.deepcopy(self.core)
        config['services']['dsh'].update(
            environment={'EXAMPLE_GREETING': 'hello'}, cpus=2.0, mem_limit='536870912',
            mem_reservation='268435456', pids_limit=4096, shm_size='2147483648',
            tmpfs=['/tmp:size=4g,exec,mode=1777,nosuid,nodev',
                   '/var/tmp:size=512m,noexec,mode=1777,nosuid,nodev'])
        config['volumes']['home'] = {'name': 'existing-home', 'external': True}
        dsh.validate_compose(config, self.core)

    def test_isolation_sensitive_service_overrides_rejected(self):
        overrides = {
            'security_opt': ['no-new-privileges:true', 'seccomp:unconfined'],
            'privileged': True, 'userns_mode': 'host', 'cgroup': 'host', 'pid': 'host',
            'ipc': 'host', 'network_mode': 'host', 'runtime': 'custom',
            'device_cgroup_rules': ['a *:* rwm'], 'devices': ['/dev/kvm:/dev/kvm'],
            'gpus': 'all', 'sysctls': {'kernel.shm_rmid_forced': '0'},
            'cap_add': ['SYS_ADMIN'], 'group_add': ['0'], 'use_api_socket': True,
            'uts': 'host', 'init': False, 'read_only': False, 'user': '0',
            'working_dir': '/', 'command': [], 'entrypoint': ['/bin/sh'],
            'volumes_from': ['another-container'], 'deploy': {'replicas': 2},
            'ports': [{'host_ip': '0.0.0.0', 'published': '11111', 'target': 3081}],
        }
        for key, value in overrides.items():
            with self.subTest(key=key):
                config = copy.deepcopy(self.core)
                config['services']['dsh'][key] = value
                with self.assertRaises(ValueError):
                    dsh.validate_compose(config, self.core)

    def test_extra_security_options_rejected(self):
        for option in ['apparmor:unconfined', 'label:disable', 'no-new-privileges:false']:
            with self.subTest(option=option):
                config = copy.deepcopy(self.core)
                config['services']['dsh']['security_opt'].append(option)
                with self.assertRaises(ValueError):
                    dsh.validate_compose(config, self.core)

    def test_tmpfs_cannot_overlay_system_paths_or_relax_mount_flags(self):
        for mount in ['/etc:mode=1777', '/proc', '/opt/dsh', '/home/agent', '/workspace',
                      '/tmp:size=2g,exec,mode=1777,suid,nodev',
                      '/tmp:size=2g,exec,mode=0777,nosuid,nodev',
                      '/tmp:size=2g,exec,mode=1777,nosuid,dev']:
            with self.subTest(mount=mount):
                config = copy.deepcopy(self.core)
                config['services']['dsh']['tmpfs'][0] = mount
                with self.assertRaises(ValueError):
                    dsh.validate_compose(config, self.core)
        for temporary in [[], self.core['services']['dsh']['tmpfs'] * 2]:
            config = copy.deepcopy(self.core)
            config['services']['dsh']['tmpfs'] = temporary
            with self.assertRaises(ValueError):
                dsh.validate_compose(config, self.core)

    def test_networks_mounts_and_volume_drivers_cannot_bypass_checks(self):
        config = copy.deepcopy(self.core)
        config['networks']['default']['driver'] = 'host'
        with self.assertRaises(ValueError):
            dsh.validate_compose(config, self.core)
        config = copy.deepcopy(self.core)
        config['services']['dsh']['volumes'][0]['source'] = 'another-volume'
        with self.assertRaises(ValueError):
            dsh.validate_compose(config, self.core)
        config = copy.deepcopy(self.core)
        config['volumes']['home']['driver_opts'] = {'type': 'none', 'o': 'bind', 'device': '/'}
        with self.assertRaises(ValueError):
            dsh.validate_compose(config, self.core)

    def test_unlimited_resource_overrides_rejected(self):
        for key, value in [('pids_limit', -1), ('mem_limit', '0'), ('cpus', 0), ('cpus', 'nan')]:
            with self.subTest(key=key, value=value):
                config = copy.deepcopy(self.core)
                config['services']['dsh'][key] = value
                with self.assertRaises(ValueError):
                    dsh.validate_compose(config, self.core)
        for key in ('pids_limit', 'shm_size'):
            with self.subTest(removed=key):
                config = copy.deepcopy(self.core)
                del config['services']['dsh'][key]
                with self.assertRaises(ValueError):
                    dsh.validate_compose(config, self.core)


if __name__ == '__main__':
    unittest.main()
