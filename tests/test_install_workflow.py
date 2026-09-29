import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_workflow


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        (self.source / 'references').mkdir(parents=True)
        (self.source / 'SKILL.md').write_text('canonical v1')
        self.destination = self.root / 'installed' / 'dsh-work'

    def install(self):
        install_workflow.install_skill(self.source, self.destination, self.root / 'dsh-task.cmd')

    def test_update_replaces_local_edits_and_removes_stale_files(self):
        self.install()
        (self.destination / 'SKILL.md').write_text('local fork')
        (self.destination / 'obsolete.txt').write_text('stale instructions')
        (self.source / 'SKILL.md').write_text('canonical v2')
        self.install()
        self.assertEqual((self.destination / 'SKILL.md').read_text(), 'canonical v2')
        self.assertFalse((self.destination / 'obsolete.txt').exists())
        self.assertFalse((self.source / '.containerize-dsh-install').exists())
        self.assertEqual((self.destination / 'references/launcher.txt').read_text().strip(),
                         str(self.root / 'dsh-task.cmd'))

    def test_failed_copy_preserves_previous_installation(self):
        self.install()
        with patch.object(install_workflow.shutil, 'copytree', side_effect=OSError('copy failed')):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual((self.destination / 'SKILL.md').read_text(), 'canonical v1')

    def test_unmanaged_installation_is_preserved(self):
        self.destination.mkdir(parents=True)
        (self.destination / 'SKILL.md').write_text('unmanaged')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            self.install()
        self.assertEqual((self.destination / 'SKILL.md').read_text(), 'unmanaged')

    def test_source_cannot_be_installation_destination(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            install_workflow.install_skill(self.source, self.source, self.root / 'launcher')

    def test_invalid_claude_frontmatter_preserves_previous_installation(self):
        self.install()
        with self.assertRaisesRegex(ValueError, 'frontmatter'):
            install_workflow.install_skill(self.source, self.destination,
                                           self.root / 'launcher', claude_code=True)
        self.assertEqual((self.destination / 'SKILL.md').read_text(), 'canonical v1')
        self.assertFalse(list(self.destination.parent.glob('.dsh-work-update-*')))


class WorkflowInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.codex = self.home / '.codex' / 'skills' / 'dsh-work'
        self.claude = self.home / '.claude' / 'skills' / 'dsh-work'
        self.launcher = self.home / '.local' / 'bin' / 'dsh-task'
        self.source = install_workflow.ROOT / 'skills' / 'dsh-work'
        self.output = io.StringIO()
        patches = (
            patch.object(Path, 'home', return_value=self.home),
            patch.dict(install_workflow.os.environ, {}, clear=True),
            patch.object(install_workflow.sys, 'platform', 'linux'),
            contextlib.redirect_stdout(self.output),
        )
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for context in patches:
            stack.enter_context(context)

    def assert_skills_installed(self):
        original = (self.source / 'SKILL.md').read_text(encoding='utf-8')
        codex = (self.codex / 'SKILL.md').read_text(encoding='utf-8')
        claude = (self.claude / 'SKILL.md').read_text(encoding='utf-8')
        self.assertEqual(codex, original)
        self.assertEqual((self.codex / 'agents' / 'openai.yaml').read_bytes(),
                         (self.source / 'agents' / 'openai.yaml').read_bytes())
        _, frontmatter, claude_body = claude.split('---\n', 2)
        self.assertIn('disable-model-invocation: true\n', frontmatter)
        self.assertIn('name: dsh-work\n', frontmatter)
        self.assertEqual(claude_body, original.split('---\n', 2)[2])
        self.assertFalse((self.claude / 'agents').exists())
        self.assertNotIn('disable-model-invocation:', original)
        for skill in (self.codex, self.claude):
            self.assertEqual((skill / 'references' / 'launcher.txt').read_text().strip(),
                             str(self.launcher))
            self.assertEqual((skill / '.containerize-dsh-install').read_text(),
                             str(install_workflow.ROOT))
            self.assertIn(str(skill), self.output.getvalue())
        self.assertTrue(self.launcher.is_file())

    def test_installs_both_skills_at_default_user_locations(self):
        install_workflow.main()
        self.assert_skills_installed()

    def test_respects_custom_config_directories(self):
        self.codex = self.root / 'custom codex' / 'skills' / 'dsh-work'
        self.claude = self.root / 'custom claude' / 'skills' / 'dsh-work'
        with patch.dict(install_workflow.os.environ, {
            'CODEX_HOME': str(self.codex.parents[1]),
            'CLAUDE_CONFIG_DIR': str(self.claude.parents[1]),
        }):
            install_workflow.main()
        self.assert_skills_installed()
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / '.claude').exists())

    def test_windows_installs_both_skills_with_cmd_launcher(self):
        localappdata = self.root / 'local app data'
        self.launcher = localappdata / 'containerize-dsh' / 'bin' / 'dsh-task.cmd'
        winreg = MagicMock()
        winreg.QueryValueEx.return_value = ('', winreg.REG_EXPAND_SZ)
        with patch.object(install_workflow.sys, 'platform', 'win32'), \
                patch.dict(install_workflow.os.environ, {'LOCALAPPDATA': str(localappdata)}), \
                patch.dict(sys.modules, {'winreg': winreg}), \
                patch('ctypes.windll', create=True):
            install_workflow.main()
        self.assert_skills_installed()
        self.assertIn('dsh_task.py', self.launcher.read_text())
        self.assertIn('%*', self.launcher.read_text())
        self.assertEqual(winreg.SetValueEx.call_args.args[-1], ';' + str(self.launcher.parent))

    def test_updates_both_managed_copies(self):
        install_workflow.main()
        for skill in (self.codex, self.claude):
            (skill / 'SKILL.md').write_text('local fork')
            (skill / 'obsolete.txt').write_text('stale instructions')
        install_workflow.main()
        self.assert_skills_installed()
        for skill in (self.codex, self.claude):
            self.assertFalse((skill / 'obsolete.txt').exists())

    def test_unmanaged_claude_skill_blocks_changes_to_codex_and_launcher(self):
        install_workflow.main()
        (self.claude / '.containerize-dsh-install').unlink()
        (self.claude / 'SKILL.md').write_text('unmanaged')
        (self.codex / 'SKILL.md').write_text('local fork')
        self.launcher.write_text('# containerize-dsh launcher\n# existing wrapper\n')
        with self.assertRaisesRegex(ValueError, 'unmanaged'):
            install_workflow.main()
        self.assertEqual((self.claude / 'SKILL.md').read_text(), 'unmanaged')
        self.assertEqual((self.codex / 'SKILL.md').read_text(), 'local fork')
        self.assertEqual(self.launcher.read_text(),
                         '# containerize-dsh launcher\n# existing wrapper\n')

    def test_shared_config_directory_is_rejected_before_writing(self):
        shared = str(self.root / 'shared')
        with patch.dict(install_workflow.os.environ, {
            'CODEX_HOME': shared, 'CLAUDE_CONFIG_DIR': shared,
        }):
            with self.assertRaisesRegex(ValueError, 'separate installation directories'):
                install_workflow.main()
        self.assertFalse(Path(shared).exists())
        self.assertFalse(self.launcher.exists())


if __name__ == '__main__':
    unittest.main()
