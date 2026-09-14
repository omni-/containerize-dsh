from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh


class ProfileTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.registry = self.root / 'profiles.json'
        patched = patch.object(dsh, 'profiles_path', return_value=self.registry)
        patched.start()
        self.addCleanup(patched.stop)

    def save(self, profile):
        self.registry.write_text(json.dumps({'sample': profile}), encoding='utf-8')

    def rejects(self, argv, message):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as error:
            dsh.parse_args(argv)
        self.assertEqual(error.exception.code, 2)
        self.assertIn(message, output.getvalue())

    def test_no_profile_never_reads_registry(self):
        self.registry.write_text('invalid JSON')
        with patch.object(dsh, 'load_profile') as load:
            args = dsh.parse_args(['up'])
        load.assert_not_called()
        self.assertEqual((args.name, args.port, args.plugin, args.key_file),
                         ('dsh-sandbox', 11111, None, None))

    def test_selected_profile_supplies_transfer_and_sandbox_defaults(self):
        profile = dict(plugin=str(self.root / 'external plugin'), repo=str(self.root / 'repo'),
                       name='saved-sandbox', port=12345, key_file=str(self.root / 'key'))
        self.save(profile)
        for action in ['init', 'refresh', 'status', 'import']:
            with self.subTest(action=action):
                argv = ['--profile', 'sample', action]
                if action == 'import':
                    argv += ['--bundle', 'work.bundle']
                args = dsh.parse_args(argv)
                for key, value in profile.items():
                    self.assertEqual(getattr(args, key), value)

    def test_explicit_arguments_override_every_saved_field(self):
        self.save(dict(plugin=str(self.root), repo=str(self.root), name='saved',
                       port=12345, key_file=str(self.root / 'key')))
        args = dsh.parse_args(['--profile', 'sample', '--plugin', 'explicit-plugin',
                              '--name', 'dsh-sandbox', '--port', '11111',
                              '--key-file', 'explicit-key', 'init', '--repo', 'explicit-repo'])
        self.assertEqual((args.plugin, args.name, args.port, args.key_file, args.repo),
                         ('explicit-plugin', 'dsh-sandbox', 11111, 'explicit-key', 'explicit-repo'))

    def test_partial_profile_keeps_defaults_and_requires_repo(self):
        self.save({})
        args = dsh.parse_args(['--profile', 'sample', 'status'])
        self.assertEqual((args.name, args.port, args.repo), ('dsh-sandbox', 11111, None))
        self.rejects(['--profile', 'sample', 'init'], '--repo is required')
        self.rejects(['init'], '--repo is required')

    def test_missing_unknown_and_malformed_registry_fail_before_execution(self):
        self.rejects(['--profile', 'sample', 'up'], 'Cannot read profile registry')
        self.save({})
        self.rejects(['--profile', 'unknown', 'up'], 'Unknown profile')
        for contents in ['{', '[]']:
            self.registry.write_text(contents)
            self.rejects(['--profile', 'sample', 'up'], 'registry')

    def test_rejects_secrets_unsupported_fields_and_invalid_values(self):
        for profile in [dict(api_key='secret'), dict(command='run'), [],
                        dict(port=True), dict(port='123'), dict(port=0), dict(port=65536),
                        dict(plugin='relative/path'), dict(repo=None), dict(key_file=''),
                        dict(name='invalid name')]:
            with self.subTest(profile=profile):
                self.save(profile)
                self.rejects(['--profile', 'sample', 'up'], 'error:')

    def test_expands_home_without_resolving_against_checkout(self):
        self.save(dict(plugin='~/private-plugin'))
        args = dsh.parse_args(['--profile', 'sample', 'up'])
        self.assertEqual(args.plugin, str(Path.home() / 'private-plugin'))

    def test_registry_inside_public_repo_is_rejected(self):
        with patch.object(dsh, 'profiles_path', return_value=dsh.ROOT / 'profiles.json'):
            self.rejects(['--profile', 'sample', 'up'], 'outside the public repository')


class ConfigPathTests(unittest.TestCase):
    def test_platform_config_locations(self):
        home = Path.home()
        cases = [
            ('win32', {}, home / 'AppData' / 'Roaming'),
            ('win32', {'APPDATA': str(home / 'roaming')}, home / 'roaming'),
            ('darwin', {}, home / 'Library' / 'Application Support'),
            ('linux', {}, home / '.config'),
            ('linux', {'XDG_CONFIG_HOME': str(home / 'xdg')}, home / 'xdg'),
            ('linux', {'XDG_CONFIG_HOME': 'relative'}, home / '.config'),
        ]
        for platform, env, base in cases:
            with self.subTest(platform=platform, env=env), \
                    patch.object(sys, 'platform', platform), patch.dict(os.environ, env, clear=True), \
                    patch.object(Path, 'home', return_value=home):
                self.assertEqual(dsh.profiles_path(), base / 'containerize-dsh' / 'profiles.json')
