from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
