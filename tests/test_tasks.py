"""Synthetic Git fixtures and fake Docker resources; never touches active tasks."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh_task as taskmod
from git_transfer import git, commit, make_bundle


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'host'
        self.repo.mkdir()
        git(self.repo, 'init', '-b', 'main')
        git(self.repo, 'config', 'user.name', 'Test')
        git(self.repo, 'config', 'user.email', 'test@localhost')
        (self.repo / 'file').write_text('base\n')
        (self.repo / '.gitignore').write_text('ignored\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'base')
        self.head = git(self.repo, 'rev-parse', 'HEAD')
        self.directory = self.root / 'task-state'
        self.directory.mkdir()
        self.task = dict(id='test-' + 'a' * 32, repo=str(self.repo), repository=taskmod.identity(self.repo),
                         target='main', source_head=self.head, transfer_host=self.head, base=self.head,
                         resolved=dict(name='fixture', port=11111, plugin=None, key_file=None),
                         volumes=dict(workspace='fixture_workspace', home='fixture_home'), reviews=[])
        self.box = Mock()

    def review(self, dirty_base=False, agent_content='agent\n', ignored_file=False):
        if dirty_base:
            (self.repo / 'file').write_text('user baseline\n')
            self.task['base'], _ = commit(self.repo, snapshot=True)
        agent = self.root / ('agent-' + str(len(self.task['reviews'])))
        git(self.repo, 'worktree', 'add', '--detach', agent, self.task['base'])
        if ignored_file:
            (agent / 'ignored').write_text('agent tracked file\n')
            git(agent, 'add', '-f', 'ignored')
            git(agent, 'commit', '-m', 'intentionally track ignored file')
        (agent / 'file').write_text(agent_content)
        revision, _ = commit(agent, snapshot=True, untracked=True)
        bundle = self.directory / (agent.name + '.bundle')
        make_bundle(self.repo, bundle, dict(base=self.task['base'], host=self.head, work=revision))
        branch = 'dsh/review-' + agent.name
        taskmod.dsh.transfer(None, argparse.Namespace(action='import', repo=str(self.repo),
                                                     bundle=str(bundle), branch=branch))
        audit = dict(base=self.task['base'], host=self.head, head=self.task['base'],
                     tree=git(self.repo, 'rev-parse', revision + '^{tree}'), index='', new='', ignored='', refs=[self.task['base']])
        record = dict(revision=revision, branch=branch, bundle=str(bundle),
                      sha256=taskmod.hashlib.sha256(bundle.read_bytes()).hexdigest(), audit=audit, report='reviewed')
        self.task['reviews'].append(record)
        taskmod.save(self.directory, self.task)
        git(self.repo, 'worktree', 'remove', '--force', agent)  # disposable fixture only
        return record

    def checks(self):
        path = self.directory / 'checks.txt'
        path.write_text('Synthetic fixture verified')
        return path

    def test_start_optional_profile_resolution(self):
        subdir = self.repo / 'subdirectory'
        subdir.mkdir()
        cases = [
            ([], subdir, None, 11111),
            (['--repo', str(self.repo), '--port', '12346'], self.root, None, 12346),
            (['--profile', 'saved'], self.root, 'saved', 12345),
            (['--profile', 'saved', '--repo', str(self.repo), '--port', '12346'],
             self.root, 'saved', 12346),
        ]
        for argv, cwd, selected, port in cases:
            with self.subTest(argv=argv):
                box = Mock()
                box.env = {'DSH_IMAGE': 'dsh-sandbox:local'}
                box.compose.return_value = 'container-id'
                with patch.object(taskmod, 'state_root', return_value=self.root / 'tasks'), \
                        patch.object(Path, 'cwd', return_value=cwd), \
                        patch.object(taskmod.dsh, 'load_profile', return_value={
                            'repo': str(self.repo) if not '--repo' in argv else str(self.root / 'unused'),
                            'port': 12345}) as load, \
                        patch.object(taskmod, 'sandbox') as sandbox, \
                        patch.object(taskmod, 'docker_list', return_value=[]), \
                        patch.object(taskmod, 'audit', return_value={'base': self.head, 'host': self.head}), \
                        patch.object(taskmod.dsh, 'transfer') as transfer, \
                        patch.object(taskmod.dsh, 'run', return_value='[{"CreatedAt":"now","Id":"container-id"}]'), \
                        patch.object(taskmod, 'show_url'):
                    sandbox.return_value.__enter__.return_value = box
                    taskmod.main(['start', '--task', 'fixture', *argv])
                    task = sandbox.call_args.args[0]
                    self.assertEqual(task['repo'], str(self.repo.resolve()))
                    self.assertEqual(task['profile'], selected)
                    self.assertEqual(task['resolved']['port'], port)
                    self.assertIsNone(task['resolved']['plugin'])
                    self.assertIsNone(task['resolved']['key_file'])
                    self.assertEqual(transfer.call_args.args[1].repo, str(self.repo.resolve()))
                    if selected:
                        load.assert_called_once_with(selected)
                    else:
                        load.assert_not_called()

    def test_clean_integration_after_host_advancement(self):
        review = self.review()
        (self.repo / 'host-new').write_text('host advancement')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'host advances')
        prepared = taskmod.prepare(self.directory, self.task)
        self.assertEqual((self.repo / 'file').read_text(), 'base\n')
        taskmod.finalize(self.directory, self.task, self.checks())
        self.assertEqual((self.repo / 'file').read_text(), 'agent\n')
        self.assertTrue((self.repo / 'host-new').exists())
        self.assertEqual(self.task['integrated']['review'], review['revision'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), prepared['revision'])

    def test_dirty_target_preserves_index_and_files_and_blocks_cleanup(self):
        self.review()
        prepared = taskmod.prepare(self.directory, self.task)
        (self.repo / 'file').write_text('staged\n')
        git(self.repo, 'add', 'file')
        (self.repo / 'file').write_text('unstaged\n')
        index = (self.repo / '.git/index').read_bytes()
        with self.assertRaisesRegex(ValueError, 'Integration is incomplete'):
            taskmod.finalize(self.directory, self.task, self.checks())
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)
        self.assertEqual((self.repo / 'file').read_text(), 'unstaged\n')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)
        self.assertTrue(Path(prepared['worktree']).exists())
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            taskmod.cleanup(self.directory, self.task, self.box)

    def test_snapshot_dependency_conflicts_without_importing_user_edits(self):
        self.review(dirty_base=True, agent_content='agent built on user baseline\n')
        index = (self.repo / '.git/index').read_bytes()
        with self.assertRaisesRegex(ValueError, 'dirty baseline'):
            taskmod.prepare(self.directory, self.task)
        prepared = self.task['integrations'][-1]
        self.assertEqual(prepared['status'], 'blocked')
        self.assertEqual(git(prepared['worktree'], 'rev-parse', 'HEAD'), self.head)
        self.assertEqual((self.repo / 'file').read_text(), 'user baseline\n')
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)

    def test_target_advancement_conflict_retains_prepared_branch(self):
        self.review()
        (self.repo / 'file').write_text('conflicting host\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'conflict')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            taskmod.prepare(self.directory, self.task)
        self.assertTrue(Path(self.task['integrations'][-1]['worktree']).exists())

    def test_finalize_never_overwrites_ignored_host_file(self):
        self.review(ignored_file=True)
        taskmod.prepare(self.directory, self.task)
        (self.repo / 'ignored').write_text('valuable host artifact\n')
        git(self.repo, 'config', 'merge.autoStash', 'true')
        with self.assertRaises(RuntimeError):
            taskmod.finalize(self.directory, self.task, self.checks())
        self.assertEqual((self.repo / 'ignored').read_text(), 'valuable host artifact\n')
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(git(self.repo, 'stash', 'list'), '')

    def test_repeated_reviews_preserve_both_bundles_and_refs(self):
        first = self.review()
        second = self.review(agent_content='agent v2\n')
        self.assertNotEqual(first['revision'], second['revision'])
        for review in self.task['reviews']:
            self.assertTrue(Path(review['bundle']).exists())
            self.assertEqual(git(self.repo, 'rev-parse', review['branch']), review['revision'])
        self.assertEqual(taskmod.latest(self.task), second)

    def test_export_refuses_new_files_without_opt_in(self):
        with patch.object(taskmod, 'owned'), patch.object(taskmod, 'audit', return_value={
                'base': self.head, 'host': self.head, 'new': 'new-file'}), patch.object(taskmod.dsh, 'transfer') as transfer:
            with self.assertRaisesRegex(ValueError, '--include-untracked'):
                taskmod.export_task(self.directory, self.task, self.box)
            transfer.assert_not_called()
            self.box.compose.assert_called_once_with('stop')

    def test_cleanup_refuses_changed_ignored_or_unpreserved_refs(self):
        review = self.review()
        with patch.object(taskmod, 'owned'), patch.object(taskmod, 'docker_list', return_value=['fixture_workspace']), \
                patch.object(taskmod.dsh, 'run') as run:
            changed = {**review['audit'], 'tree': 'changed'}
            with patch.object(taskmod, 'audit', return_value=changed), self.assertRaisesRegex(ValueError, 'changed'):
                taskmod.cleanup(self.directory, self.task, self.box)
            review['audit']['ignored'] = 'valuable.bin'
            with patch.object(taskmod, 'audit', return_value=review['audit']), self.assertRaisesRegex(ValueError, 'Ignored'):
                taskmod.cleanup(self.directory, self.task, self.box)
            run.assert_not_called()

    def test_cleanup_retains_recovery_and_is_idempotent(self):
        review = self.review()
        with patch.object(taskmod, 'owned'), patch.object(taskmod, 'docker_list', return_value=['fixture_workspace']), \
                patch.object(taskmod, 'audit', return_value=review['audit']), patch.object(taskmod.dsh, 'run') as run:
            taskmod.cleanup(self.directory, self.task, self.box)
            taskmod.cleanup(self.directory, self.task, self.box)
            run.assert_called_once_with('docker', 'volume', 'rm', 'fixture_workspace')
        self.assertTrue(Path(review['bundle']).exists())
        self.assertTrue((self.directory / 'manifest.json').exists())

    def test_changed_bundle_refuses_integration_and_cleanup(self):
        review = self.review()
        Path(review['bundle']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'bundle changed'):
            taskmod.prepare(self.directory, self.task)
        with self.assertRaisesRegex(ValueError, 'bundle changed'):
            taskmod.cleanup(self.directory, self.task, self.box)

    def test_foreign_volume_consumer_refuses_ownership(self):
        self.task['volume_created'] = dict(workspace='now', home='now')
        volume = dict(CreatedAt='now', Labels={'com.docker.compose.project': 'fixture',
                                             'com.docker.compose.volume': 'workspace'})
        with patch.object(taskmod, 'docker_list', side_effect=[[], ['fixture_workspace'], ['foreign']]), \
                patch.object(taskmod.dsh, 'run', side_effect=[json.dumps([volume]), json.dumps([{'Id': 'foreign'}])]):
            with self.assertRaisesRegex(ValueError, 'unrecorded container'):
                taskmod.owned(self.task)

    def test_lock_excludes_concurrent_operations_and_releases_on_failure(self):
        directory = self.root / self.task['id']
        directory.mkdir()
        taskmod.save(directory, self.task)
        with patch.object(taskmod, 'state_root', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'fixture failure'):
                with taskmod.locked(self.task['id']):
                    with self.assertRaises(FileExistsError):
                        with taskmod.locked(self.task['id']):
                            self.fail('Concurrent operation acquired lock')
                    raise ValueError('fixture failure')
            self.assertFalse((directory / 'lock').exists())
            with taskmod.locked(self.task['id']):
                pass


if __name__ == '__main__':
    unittest.main()
