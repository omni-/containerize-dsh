from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh
import dsh_task


class ExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.bundle = Path(temp.name) / 'work.bundle'
        self.sandbox = object.__new__(dsh.Sandbox)
        self.sandbox.compose = Mock(return_value='running-container')

    def args(self, *options):
        return dsh.parse_args(['export', '--bundle', str(self.bundle), *options])

    def test_running_sandbox_requires_force(self):
        with patch.object(dsh, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Stop the sandbox'):
                dsh.transfer(self.sandbox, self.args())
        run.assert_not_called()
        self.assertFalse(self.bundle.exists())

    def test_force_exports_and_removes_only_transfer_container(self):
        def copy_bundle(command, *, stdout, check):
            self.assertEqual(command[-2:], ['cat', '/tmp/output.bundle'])
            stdout.write(b'bundle contents')

        with patch.object(dsh, 'run') as run, \
                patch.object(dsh.subprocess, 'run', side_effect=copy_bundle):
            dsh.transfer(self.sandbox, self.args('--force', '--snapshot', '--include-untracked'))
        self.assertEqual(self.bundle.read_bytes(), b'bundle contents')
        self.sandbox.compose.assert_called_once()
        launch = self.sandbox.compose.call_args.args
        self.assertEqual(launch[:4], ('run', '-d', '--no-deps', '--name'))
        container = launch[4]
        self.assertTrue(container.startswith('dsh-transfer-'))
        self.assertEqual(run.call_args_list[0].args,
                         ('docker', 'exec', container, 'python3', '/opt/dsh/workspace.py',
                          'export', '--bundle', '/tmp/output.bundle', '--snapshot', '--include-untracked'))
        run.assert_called_with('docker', 'rm', '-f', container, capture=True)

    def test_force_does_not_overwrite_bundle(self):
        self.bundle.write_bytes(b'existing bundle')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            dsh.transfer(self.sandbox, self.args('--force'))
        self.sandbox.compose.assert_not_called()
        self.assertEqual(self.bundle.read_bytes(), b'existing bundle')

    def test_other_workspace_commands_reject_force(self):
        for action in ('init', 'refresh', 'status', 'import'):
            with self.subTest(action=action), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                dsh.parse_args([action, '--repo', '.', '--force', '--bundle', 'work.bundle']
                               if action == 'import' else [action, '--repo', '.', '--force'])


class TaskBundleExportTests(unittest.TestCase):
    def test_direct_export_forwards_flags_without_review_or_stop(self):
        task = dict(resolved={}, reviews=[])
        box = Mock()
        with patch.object(dsh_task, 'locked', return_value=nullcontext((Path('.'), task))), \
                patch.object(dsh_task, 'sandbox', return_value=nullcontext(box)), \
                patch.object(dsh_task, 'owned') as owned, \
                patch.object(dsh_task.dsh, 'transfer') as transfer, \
                patch.object(dsh_task, 'export_task') as review, redirect_stdout(io.StringIO()):
            dsh_task.main(['export', 'chosen-task', '--force', '--snapshot',
                           '--include-untracked', '--bundle', 'work.bundle'])
        owned.assert_called_once_with(task)
        review.assert_not_called()
        box.compose.assert_not_called()
        self.assertEqual(task['reviews'], [])
        transfer.assert_called_once()
        passed_box, args = transfer.call_args.args
        self.assertIs(passed_box, box)
        self.assertEqual(vars(args), dict(action='export', force=True, snapshot=True,
                                         include_untracked=True, bundle='work.bundle'))

    def test_current_repository_selection_requires_unique_active_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = str(root / 'repo.git')
            def record(name, **fields):
                directory = root / name
                directory.mkdir()
                (directory / 'manifest.json').write_text(json.dumps(
                    dict(id=name, repository=repository, **fields)), encoding='utf-8')
            record('cleaned', cleaned=True)
            record('discarding', discard_started=True)
            with patch.object(dsh_task, 'state_root', return_value=root), \
                    patch.object(dsh_task, 'identity', return_value=repository):
                with self.assertRaisesRegex(ValueError, 'Specify a task ID'):
                    dsh_task.task_for_repository()
                record('active')
                self.assertEqual(dsh_task.task_for_repository(), 'active')
                record('another')
                with self.assertRaisesRegex(ValueError, 'Specify a task ID'):
                    dsh_task.task_for_repository()

    def test_force_without_bundle_rejected_before_task_access(self):
        with patch.object(dsh_task, 'locked') as locked, redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            dsh_task.main(['export', 'chosen-task', '--force'])
        locked.assert_not_called()
