import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dsh
import dsh_task


def core_config():
    return {
        'name': 'fixture',
        'services': {'dsh': {
            'image': 'dsh-sandbox:local', 'user': '10001:10001', 'read_only': True,
            'environment': {'CORE': 'original'}, 'pids_limit': 2048, 'shm_size': '1073741824',
            'tmpfs': ['/tmp:size=2g,exec,mode=1777,nosuid,nodev',
                      '/var/tmp:size=1g,exec,mode=1777,nosuid,nodev'],
        }},
        'volumes': {'home': {'name': 'fixture_home'}, 'workspace': {'name': 'fixture_workspace'}},
    }


class ComposeCompositionTests(unittest.TestCase):
    def setUp(self):
        self.core = core_config()

    def contribution(self, name, service=None, volumes=None):
        raw = {'services': {'dsh': service or {}}}
        config = copy.deepcopy(self.core)
        for key, value in (service or {}).items():
            if key == 'environment':
                config['services']['dsh'][key].update(value)
            else:
                config['services']['dsh'][key] = value
        if volumes:
            raw['volumes'] = volumes
            for key, value in volumes.items():
                config['volumes'][key].update(value)
        return name, raw, config

    def test_disjoint_and_identical_contributions_compose(self):
        first = self.contribution('/plugins/one', {'environment': {'ONE': '1', 'SAME': 'value'}, 'cpus': 2})
        second = self.contribution('/plugins/two', {'environment': {'TWO': '2', 'SAME': 'value'}, 'cpus': 2,
                                                   'mem_limit': '536870912'})
        result = dsh.compose_plugins(self.core, [first, second])
        self.assertEqual(result['services']['dsh']['environment'],
                         {'CORE': 'original', 'ONE': '1', 'TWO': '2', 'SAME': 'value'})
        self.assertEqual(result['services']['dsh']['cpus'], 2)
        self.assertEqual(result['services']['dsh']['mem_limit'], '536870912')
        self.assertEqual(self.core, core_config())

    def test_conflicts_identify_both_plugins_and_field(self):
        for field, first, second in (
            ('environment.SHARED', {'environment': {'SHARED': 'a'}}, {'environment': {'SHARED': 'b'}}),
            ('pids_limit', {'pids_limit': 2048}, {'pids_limit': 4096}),
            ('cpus', {'cpus': 2}, {'cpus': 3}),
            ('tmpfs', {'tmpfs': self.core['services']['dsh']['tmpfs']},
             {'tmpfs': ['/tmp:size=4g,exec,mode=1777,nosuid,nodev',
                        '/var/tmp:size=1g,exec,mode=1777,nosuid,nodev']}),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError) as error:
                    dsh.compose_plugins(self.core, [self.contribution('/one', first), self.contribution('/two', second)])
                self.assertIn('/one and /two', str(error.exception))
                self.assertIn(field, str(error.exception))

    def test_explicit_core_value_is_still_a_claim(self):
        with self.assertRaisesRegex(ValueError, 'environment.CORE'):
            dsh.compose_plugins(self.core, [self.contribution('/one', {'environment': {'CORE': 'original'}}),
                                           self.contribution('/two', {'environment': {'CORE': 'changed'}})])

    def test_volume_definitions_are_atomic_and_final_separation_is_checked(self):
        with self.assertRaisesRegex(ValueError, '/one and /two.*volumes.home'):
            dsh.compose_plugins(self.core, [self.contribution('/one', volumes={'home': {'name': 'first'}}),
                                           self.contribution('/two', volumes={'home': {'name': 'second'}})])
        with self.assertRaisesRegex(ValueError, 'separate volumes'):
            dsh.compose_plugins(self.core, [self.contribution('/one', volumes={'home': {'name': 'shared'}}),
                                           self.contribution('/two', volumes={'workspace': {'name': 'shared'}})])

    def test_each_override_is_validated_before_composition(self):
        with self.assertRaisesRegex(ValueError, 'isolation'):
            dsh.compose_plugins(self.core, [self.contribution('/one', {'read_only': False}),
                                           self.contribution('/two', {'read_only': True})])


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plugin(self, name, build=True, runtime=True, dockerfile=None):
        directory = self.root / name
        directory.mkdir()
        manifest = {'version': 1}
        if runtime:
            (directory / 'runtime').mkdir()
            manifest['runtime'] = 'runtime'
        if build:
            manifest['build'] = {'context': '.', 'dockerfile': 'Dockerfile'}
            (directory / 'Dockerfile').write_text(dockerfile or
                'ARG DSH_BASE_IMAGE\nFROM ${DSH_BASE_IMAGE}\nUSER 10001:10001\n', encoding='utf-8')
        (directory / 'plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
        return directory

    def args(self, plugins):
        return argparse.Namespace(name='fixture', port=11111, plugins=list(map(str, plugins)), key_file=None)

    def test_runtime_mounts_are_separate_readonly_and_ordered(self):
        paths = [self.plugin('one'), self.plugin('two'), self.plugin('three', runtime=False)]
        with patch.object(dsh, 'run', return_value=json.dumps(core_config())):
            box = dsh.Sandbox(self.args(paths), self.root)
        profile = json.loads((self.root / 'profile.json').read_text())['services']['dsh']
        mounts = profile['volumes']
        self.assertEqual([m['target'] for m in mounts], ['/opt/dsh-plugin', '/opt/dsh-plugins/2'])
        self.assertEqual([m['source'] for m in mounts], [str(p / 'runtime') for p in paths[:2]])
        self.assertTrue(all(m['read_only'] and not m['bind']['create_host_path'] for m in mounts))
        self.assertEqual(json.loads(profile['environment']['DSH_PLUGIN_RUNTIMES']), [
            {'plugin': str(paths[0]), 'runtime': '/opt/dsh-plugin'},
            {'plugin': str(paths[1]), 'runtime': '/opt/dsh-plugins/2'},
        ])
        self.assertEqual([p.directory for p in box.plugins], paths)

    def test_builds_extend_previous_image_in_profile_order(self):
        paths = [self.plugin('one'), self.plugin('runtime-only', build=False), self.plugin('two')]
        with patch.object(dsh, 'run', return_value=json.dumps(core_config())):
            box = dsh.Sandbox(self.args(paths), self.root)
        with patch.object(dsh, 'run') as run:
            box.build()
        calls = run.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertIn('DSH_BASE_IMAGE=dsh-sandbox:local', calls[1].args)
        self.assertIn('DSH_BASE_IMAGE=' + box.builds[0][1], calls[2].args)
        self.assertEqual(calls[1].args[-1], paths[0])
        self.assertEqual(calls[2].args[-1], paths[2])
        self.assertEqual(box.env['DSH_IMAGE'], box.builds[-1][1])
        with patch.object(dsh, 'run', return_value=json.dumps(core_config())):
            reverse = dsh.Sandbox(self.args(list(reversed(paths))), self.root)
        self.assertNotEqual(box.env['DSH_IMAGE'], reverse.env['DSH_IMAGE'])

    def test_single_plugin_keeps_image_tag_and_runtime_convention(self):
        directory = self.plugin('one')
        with patch.object(dsh, 'run', return_value=json.dumps(core_config())):
            box = dsh.Sandbox(self.args([directory]), self.root)
        digest = hashlib.sha256(str(directory).encode()).hexdigest()[:12]
        self.assertEqual(box.env['DSH_IMAGE'], 'dsh-plugin:' + digest)
        mounts = json.loads((self.root / 'profile.json').read_text())['services']['dsh']['volumes']
        self.assertEqual(mounts[0]['target'], '/opt/dsh-plugin')

    def test_no_plugin_has_no_runtime_mount_or_derived_build(self):
        with patch.object(dsh, 'run', return_value=json.dumps(core_config())):
            box = dsh.Sandbox(self.args([]), self.root)
        profile = json.loads((self.root / 'profile.json').read_text())['services']['dsh']
        self.assertNotIn('volumes', profile)
        self.assertEqual(box.builds, [])
        self.assertEqual(box.env['DSH_IMAGE'], 'dsh-sandbox:local')
        with patch.object(dsh, 'run') as run:
            box.preflight()
        run.assert_not_called()

    def test_preflight_retains_isolation_and_only_runtime_mounts(self):
        directory = self.plugin('one')
        core = core_config()
        core['networks'] = {'default': {'name': 'fixture_default'}}
        service = core['services']['dsh']
        service.update(networks={'default': None}, ports=[{'target': 3081, 'published': '11111'}],
                       cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
                       volumes=[{'type': 'volume', 'source': 'home', 'target': '/home/agent'},
                                {'type': 'volume', 'source': 'workspace', 'target': '/workspace'},
                                {'type': 'bind', 'source': str(directory / 'runtime'), 'target': '/opt/dsh-plugin', 'read_only': True},
                                {'type': 'bind', 'source': 'fixture-key', 'target': '/run/secrets/deepseek', 'read_only': True}])
        service['environment'].update(DEEPSEEK_API_KEY='fixture-secret', DEEPSEEK_API_KEY_FILE='/run/secrets/deepseek')
        with patch.object(dsh, 'run', return_value=json.dumps(core)):
            box = dsh.Sandbox(self.args([directory]), self.root)
        check = json.loads((self.root / 'preflight.json').read_text())
        checked = check['services']['dsh']
        self.assertNotIn('volumes', check)
        self.assertNotIn('networks', check)
        self.assertNotIn('ports', checked)
        self.assertEqual(checked['network_mode'], 'none')
        self.assertEqual([mount['target'] for mount in checked['volumes']], ['/opt/dsh-plugin'])
        for key in ('user', 'read_only', 'cap_drop', 'security_opt', 'pids_limit', 'shm_size', 'tmpfs'):
            self.assertEqual(checked[key], service[key])
        self.assertEqual(checked['environment']['DEEPSEEK_API_KEY'], '')
        self.assertNotIn('DEEPSEEK_API_KEY_FILE', checked['environment'])
        with patch.object(dsh, 'run') as run:
            box.preflight()
        self.assertEqual(list(run.call_args.args[:len(box.preflight_command)]), box.preflight_command)

    def test_unavailable_non_directory_and_duplicate_plugins_fail_before_docker(self):
        directory = self.plugin('one')
        file = self.root / 'file'
        file.write_text('file')
        for paths, message in (([self.root / 'missing'], 'unavailable'), ([file], 'not a directory'),
                               ([directory, directory / '..' / 'one'], 'Duplicate')):
            with self.subTest(paths=paths), patch.object(dsh, 'run') as run:
                with self.assertRaisesRegex(ValueError, message):
                    dsh.Sandbox(self.args(paths), self.root)
                run.assert_not_called()

    def test_build_that_resets_the_stack_fails_with_both_plugins_before_docker(self):
        first = self.plugin('one')
        second = self.plugin('two', dockerfile='ARG DSH_BASE_IMAGE\nFROM dsh-sandbox:local\n')
        with patch.object(dsh, 'run') as run:
            with self.assertRaises(ValueError) as error:
                dsh.Sandbox(self.args([first, second]), self.root)
        self.assertIn(str(first), str(error.exception))
        self.assertIn(str(second), str(error.exception))
        self.assertIn('incompatible build', str(error.exception))
        run.assert_not_called()

    def test_multistage_final_stage_must_descend_from_base(self):
        directory = self.plugin('one')
        source = directory / 'Dockerfile'
        for text, valid in [
            ('ARG DSH_BASE_IMAGE\nFROM ubuntu AS tools\nFROM ${DSH_BASE_IMAGE} AS final\n', True),
            ('ARG DSH_BASE_IMAGE\nFROM ${DSH_BASE_IMAGE} AS base\nFROM base AS final\n', True),
            ('ARG DSH_BASE_IMAGE\nFROM ${DSH_BASE_IMAGE}\nFROM 0\n', True),
            ('ARG DSH_BASE_IMAGE\nFROM ${DSH_BASE_IMAGE} AS base\nFROM ubuntu AS final\n', False),
            ('FROM ${DSH_BASE_IMAGE}\n', False),
        ]:
            with self.subTest(text=text):
                source.write_text(text)
                self.assertEqual(dsh.Plugin(str(directory)).extends_base(), valid)

    def test_manifest_paths_and_types_remain_validated(self):
        directory = self.plugin('one', build=False)
        manifest = directory / 'plugin.json'
        for value in ({'version': 1, 'runtime': '..'}, {'version': 1, 'runtime': 'plugin.json'},
                      {'version': 1, 'runtime': 2}, {'version': 1, 'build': {'args': {}}},
                      {'version': True}, []):
            with self.subTest(value=value):
                manifest.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    dsh.Plugin(str(directory))

    def test_legacy_task_records_are_normalized_without_profile_lookup(self):
        for legacy, expected in ((None, []), ('/old/plugin', ['/old/plugin'])):
            task = {'resolved': {'name': 'fixture', 'plugin': legacy}}
            with patch.object(dsh, 'load_profile') as load:
                args = dsh_task.args_for(task, 'status')
            load.assert_not_called()
            self.assertEqual(args.plugins, expected)
            self.assertNotIn('plugin', vars(args))
            self.assertIn('plugin', task['resolved'])


class CordisCompositionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node unavailable; the Docker smoke test also checks Cordis composition')
    def test_cordis_collision_and_order_contract(self):
        subprocess.run(['node', '--test', str(ROOT / 'tests' / 'plugin_runtime.test.mjs')], check=True,
                       capture_output=True, text=True)
