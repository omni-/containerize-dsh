"""Task discovery uses only disposable local manifests, without Git or Docker."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_task as tasks


class TaskListTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'tasks'
        patcher = patch.object(tasks, 'state_root', return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, name, **fields):
        task = dict(id=name + '-' + 'a' * 32, repo='/host/repo with spaces',
                    target='main', profile=None, resolved=dict(port=11111))
        task.update(fields)
        directory = self.root / task['id']
        directory.mkdir(parents=True)
        tasks.save(directory, task)
        return directory

    def listing(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch.object(tasks, 'git') as git, \
                patch.object(tasks.dsh, 'run') as run, \
                patch.object(tasks, 'locked') as locked:
            tasks.main(['list'])
        git.assert_not_called()
        run.assert_not_called()
        locked.assert_not_called()
        return output.getvalue(), errors.getvalue()

    def test_missing_and_empty_registry(self):
        self.assertEqual(self.listing(), ('No tasks found.\n', ''))
        self.assertFalse(self.root.exists())
        self.root.mkdir()
        self.assertEqual(self.listing(), ('No tasks found.\n', ''))

    def test_lists_all_states_sorted_without_changing_records(self):
        states = [('e', 'discarded', dict(discarded=True, cleaned=True, discard_started=True)),
                  ('d', 'cleaned', dict(cleaned=True)),
                  ('c', 'discard-incomplete', dict(discard_started=True, container='id')),
                  ('b', 'active', dict(container='id', profile='saved')),
                  ('a', 'incomplete', {})]
        for name, _, fields in states:
            directory = self.record(name, **fields)
            (directory / 'lock').touch()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        output, errors = self.listing()
        self.assertEqual(errors, '')
        lines = output.splitlines()
        self.assertIn('TASK ID', lines[0])
        for line, (name, state, _) in zip(lines[1:], reversed(states)):
            self.assertTrue(line.startswith(name + '-' + 'a' * 32))
            self.assertEqual(line.split()[1], state)
            self.assertIn('/host/repo with spaces', line)
            self.assertIn('main', line)
            self.assertIn('11111', line)
        self.assertEqual(len(lines), 6)
        self.assertIn('saved', output)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_bad_record_does_not_hide_valid_tasks(self):
        directory = self.record('broken')
        (directory / 'manifest.json').write_text('{invalid', encoding='utf-8')
        self.record('valid')
        output, errors = self.listing()
        self.assertIn('valid-' + 'a' * 32, output)
        self.assertNotIn('broken-', output)
        self.assertIn(str(directory / 'manifest.json'), errors)


if __name__ == '__main__':
    unittest.main()
