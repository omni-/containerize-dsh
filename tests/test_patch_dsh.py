from pathlib import Path
import runpy
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
patch = runpy.run_path(str(ROOT / 'scripts' / 'patch-dsh.py'))['patch']

ORIGINAL = 'const target = resolve(path ?? home);'
PATCHED = 'const target = resolve(path ?? process.cwd());'
PICKER = Path('node_modules/@deepseek-ai/dsh-host-directory-picker-browse/lib/index.js')
WEB_APP = Path('node_modules/@deepseek-ai/dsh-web-app')


class DirectoryBrowserPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def picker(self, parent, source=ORIGINAL):
        path = self.root / parent / PICKER
        path.parent.mkdir(parents=True)
        path.write_text(source, encoding='utf-8')
        return path

    def test_hoisted_and_nested_layouts(self):
        for parent in (Path('hoisted'), Path('nested') / WEB_APP):
            with self.subTest(parent=parent):
                source = '// directory browser\n' + ORIGINAL + '\n'
                path = self.picker(parent, source)

                patch(self.root / parent.parts[0])

                self.assertEqual(path.read_text(encoding='utf-8'), '// directory browser\n' + PATCHED + '\n')

    def test_patches_every_installed_copy(self):
        paths = [self.picker(Path()), self.picker(WEB_APP)]

        patch(self.root)

        for path in paths:
            self.assertEqual(path.read_text(encoding='utf-8'), PATCHED)

    def test_missing_package_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'directory browser not found'):
            patch(self.root)

    def test_changed_source_fails_before_writing_any_copy(self):
        valid = self.picker(Path())
        changed = self.picker(WEB_APP)
        for source in ('// upstream changed\n', ORIGINAL + '\n' + ORIGINAL, PATCHED):
            with self.subTest(source=source):
                changed.write_text(source, encoding='utf-8')

                with self.assertRaisesRegex(RuntimeError, 'directory browser changed'):
                    patch(self.root)

                self.assertEqual(valid.read_text(encoding='utf-8'), ORIGINAL)
                self.assertEqual(changed.read_text(encoding='utf-8'), source)


if __name__ == '__main__':
    unittest.main()
