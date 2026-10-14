"""Recursive transfers use real local repositories, including absorbed Git dirs."""
import argparse
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dsh
import dsh_task
from git_transfer import (git, commit, make_bundle, bundle_refs, gitlinks,
                          module_key, module_refs, validate_modules, preservation_ref)
from workspace import apply, audit


class SubmoduleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='dsh-submodules-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        leaf = self.repository('leaf-source')
        child = self.repository('child-source')
        git(child, '-c', 'protocol.file.allow=always', 'submodule', 'add', str(leaf), 'nested')
        git(child, 'commit', '-am', 'nested dependency')
        self.host = self.repository('host')
        self.path = 'vendor/tool with spaces'
        git(self.host, '-c', 'protocol.file.allow=always', 'submodule', 'add', '--name',
            'custom-name', str(child), self.path)
        git(self.host, 'commit', '-am', 'dependency')
        git(self.host, '-c', 'protocol.file.allow=always', 'submodule', 'update', '--init', '--recursive')
        self.child = self.host / self.path
        self.leaf = self.child / 'nested'
        for repo in (self.child, self.leaf):
            self.configure(repo)
        self.head = git(self.host, 'rev-parse', 'HEAD')
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()

    def configure(self, repo):
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@localhost')

    def repository(self, name):
        repo = self.root / name
        repo.mkdir()
        git(repo, 'init', '-b', 'main')
        self.configure(repo)
        (repo / 'file').write_text('base\n')
        (repo / '.gitignore').write_text('ignored\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-m', 'base')
        return repo

    def state(self):
        return [(repo, git(repo, 'rev-parse', 'HEAD'),
                 Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'index')).read_bytes())
                for repo in (self.host, self.child, self.leaf)]

    def assert_preserved(self, before):
        for repo, head, index in before:
            self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)
            self.assertEqual(Path(git(repo, 'rev-parse', '--path-format=absolute',
                                     '--git-path', 'index')).read_bytes(), index)

    def initialize(self, snapshot=False, untracked=False):
        base, head = commit(self.host, snapshot=snapshot, untracked=untracked)
        bundle = self.root / 'initial.bundle'
        make_bundle(self.host, bundle, dict(base=base, host=head))
        apply(self.workspace, bundle, True)
        return base, bundle

    def review(self, base):
        work, _ = commit(self.workspace, snapshot=True, untracked=True)
        bundle = self.root / 'review.bundle'
        make_bundle(self.workspace, bundle, dict(base=base, host=self.head, work=work))
        branch = 'dsh/review-recursive'
        with redirect_stdout(io.StringIO()):
            dsh.transfer(None, argparse.Namespace(action='import', repo=self.host,
                                                 bundle=bundle, branch=branch))
        directory = self.root / 'task-state'
        directory.mkdir()
        report = directory / 'checks.txt'
        report.write_text('Recursive fixture verified\n')
        task = dict(id='recursive-' + 'a' * 32, repo=str(self.host),
                    repository=dsh_task.identity(self.host), target='main', base=base,
                    transfer_host=self.head, resolved={}, volumes=dict(workspace='fixture_workspace'),
                    reviews=[dict(revision=work, branch=branch, bundle=str(bundle),
                                  sha256=dsh_task.hashlib.sha256(bundle.read_bytes()).hexdigest(),
                                  audit=audit(self.workspace), report=str(report))])
        dsh_task.save(directory, task)
        return directory, task, report

    def test_recursive_snapshot_bundle_restores_edits_without_changing_indexes(self):
        (self.child / 'file').write_text('staged\n')
        git(self.child, 'add', 'file')
        (self.child / 'file').write_text('unstaged\n')
        (self.leaf / 'file').write_text('nested edit\n')
        (self.leaf / 'new').write_text('new file\n')
        (self.leaf / 'ignored').write_text('private artifact\n')
        before = self.state()

        base, bundle = self.initialize(snapshot=True, untracked=True)

        self.assert_preserved(before)
        self.assertEqual((self.workspace / self.path / 'file').read_text(), 'unstaged\n')
        self.assertEqual((self.workspace / self.path / 'nested/file').read_text(), 'nested edit\n')
        self.assertEqual((self.workspace / self.path / 'nested/new').read_text(), 'new file\n')
        self.assertFalse((self.workspace / self.path / 'nested/ignored').exists())
        self.assertEqual(git(self.workspace, 'status', '--porcelain'), '')
        refs = bundle_refs(self.host, bundle, {'base', 'host'})
        self.assertEqual(set(module_refs(refs)), {self.path, self.path + '/nested'})
        self.assertNotEqual(base, self.head)
        self.assertEqual(git(self.host, 'for-each-ref', 'refs/dsh-transfer'), '')

    def test_revision_transfer_uses_pinned_commits_and_ignores_local_edits(self):
        (self.child / 'file').write_text('local edit\n')
        git(self.child, 'commit', '-am', 'local child commit')
        (self.leaf / 'file').write_text('nested uncommitted edit\n')
        before = self.state()

        base, _ = self.initialize()

        self.assertEqual(base, self.head)
        self.assertEqual((self.workspace / self.path / 'file').read_text(), 'base\n')
        self.assertEqual((self.workspace / self.path / 'nested/file').read_text(), 'base\n')
        self.assert_preserved(before)

    def test_snapshot_omits_nested_untracked_files_without_opt_in(self):
        (self.leaf / 'new').write_text('untracked\n')
        (self.leaf / 'file').write_text('tracked edit\n')

        self.initialize(snapshot=True)

        self.assertFalse((self.workspace / self.path / 'nested/new').exists())
        self.assertEqual((self.workspace / self.path / 'nested/file').read_text(), 'tracked edit\n')

    def test_recursive_snapshots_are_stable_across_different_commit_dates(self):
        (self.leaf / 'file').write_text('nested edit\n')
        with patch.dict(os.environ, GIT_AUTHOR_DATE='2001-01-01T00:00:00+0000',
                        GIT_COMMITTER_DATE='2001-01-01T00:00:00+0000'):
            first, _ = commit(self.host, snapshot=True)
        with patch.dict(os.environ, GIT_AUTHOR_DATE='2026-01-01T00:00:00+0000',
                        GIT_COMMITTER_DATE='2026-01-01T00:00:00+0000'):
            second, _ = commit(self.host, snapshot=True)
        self.assertEqual(first, second)

    def test_audit_tracks_nested_new_ignored_index_and_refs(self):
        self.initialize()
        leaf = self.workspace / self.path / 'nested'
        (leaf / 'file').write_text('agent\n')
        (leaf / 'new').write_text('new\n')
        (leaf / 'ignored').write_text('cache\n')

        state = audit(self.workspace)

        self.assertIn(self.path + '/nested/new', state['new'])
        self.assertIn(self.path + '/nested/ignored', state['ignored'])
        self.assertIn(self.path + '/nested', state['submodules'])
        self.assertNotEqual(state['submodules'][self.path + '/nested']['tree'],
                            git(leaf, 'rev-parse', 'HEAD^{tree}'))

    def test_export_import_preserves_nested_objects_and_dirty_host_checkouts(self):
        base, _ = self.initialize()
        leaf = self.workspace / self.path / 'nested'
        (leaf / 'file').write_text('agent edit\n')
        work, _ = commit(self.workspace, snapshot=True)
        bundle = self.root / 'export.bundle'
        make_bundle(self.workspace, bundle, dict(base=base, host=self.head, work=work))
        (self.child / 'file').write_text('host staged\n')
        git(self.child, 'add', 'file')
        (self.child / 'file').write_text('host unstaged\n')
        before = self.state()

        with redirect_stdout(io.StringIO()):
            dsh.transfer(None, argparse.Namespace(action='import', repo=self.host, bundle=bundle,
                                                 branch='dsh/review-submodules'))

        self.assert_preserved(before)
        self.assertEqual((self.child / 'file').read_text(), 'host unstaged\n')
        refs = bundle_refs(self.host, bundle, {'base', 'host', 'work'})
        leaf_work = refs[module_key(self.path + '/nested', 'work')][0]
        self.assertEqual(git(self.host, 'show', leaf_work + ':file'), 'agent edit')
        self.assertEqual(git(self.host, 'rev-parse', preservation_ref('dsh/review-submodules',
                             module_key(self.path + '/nested', 'work'))), leaf_work)

    def test_missing_submodule_objects_refuse_without_partial_bundle(self):
        git(self.host, 'submodule', 'deinit', '-f', '--', self.path)
        # A fresh clone has gitlinks but none of the submodule object databases.
        source = self.root / 'uninitialized'
        git(self.host, 'clone', '--no-local', str(self.host), str(source))
        bundle = self.root / 'missing.bundle'

        with self.assertRaisesRegex(ValueError, 'submodule update --init --recursive'):
            make_bundle(source, bundle, dict(base=self.head, host=self.head))

        self.assertFalse(bundle.exists())
        self.assertEqual(git(source, 'for-each-ref', 'refs/dsh-transfer'), '')

    def test_unregistered_nested_repository_is_not_silently_exported(self):
        nested = self.repository('unregistered')
        git(self.host, 'clone', str(nested), str(self.host / 'embedded'))

        with self.assertRaisesRegex(ValueError, 'registered in .gitmodules'):
            commit(self.host, snapshot=True, untracked=True)

    def test_mismatched_and_unsafe_submodule_payloads_refuse(self):
        _, bundle = self.initialize()
        refs = bundle_refs(self.workspace, bundle, {'base', 'host'})
        refs[module_key(self.path, 'base')] = (self.head, 'unused')
        with self.assertRaisesRegex(ValueError, 'mismatched submodule'):
            validate_modules(self.workspace, refs)
        for path in ('../outside', '/outside', '.git/objects', 'C:/outside', 'x\\y'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                module_key(path, 'base')

    def test_recursive_agent_only_integration_excludes_host_snapshot_and_cleans(self):
        (self.child / 'file').write_text('host baseline edit\n')
        before = self.state()
        base, _ = self.initialize(snapshot=True)
        (self.workspace / self.path / 'agent-new').write_text('agent child work\n')
        (self.workspace / self.path / 'nested/agent-new').write_text('agent nested work\n')
        directory, task, report = self.review(base)

        prepared = dsh_task.prepare(directory, task)

        work = Path(prepared['worktree'])
        self.assert_preserved(before)
        self.assertEqual((self.child / 'file').read_text(), 'host baseline edit\n')
        self.assertEqual((work / self.path / 'file').read_text(), 'base\n')
        self.assertEqual((work / self.path / 'agent-new').read_text(), 'agent child work\n')
        self.assertEqual((work / self.path / 'nested/agent-new').read_text(), 'agent nested work\n')
        with self.assertRaisesRegex(ValueError, 'Integration is incomplete'):
            dsh_task.finalize(directory, task, report)
        (self.child / 'file').rename(self.root / 'preserved-baseline')
        git(self.child, 'checkout', '--', 'file')

        dsh_task.finalize(directory, task, report)

        self.assertEqual((self.child / 'agent-new').read_text(), 'agent child work\n')
        self.assertEqual((self.leaf / 'agent-new').read_text(), 'agent nested work\n')
        self.assertEqual(git(self.host, 'status', '--porcelain', '--ignore-submodules=none'), '')
        box = Mock()
        with patch.object(dsh_task, 'owned'), \
                patch.object(dsh_task, 'docker_list', return_value=['fixture_workspace']), \
                patch.object(dsh_task, 'audit', side_effect=lambda box: audit(self.workspace)), \
                patch.object(dsh_task.dsh, 'run') as run:
            dsh_task.cleanup(directory, task, box, require_integrated=True)
        run.assert_called_once_with('docker', 'volume', 'rm', 'fixture_workspace')
        self.assertFalse(work.exists())
        self.assertEqual((self.root / 'preserved-baseline').read_text(), 'host baseline edit\n')
        self.assertTrue(Path(task['reviews'][0]['bundle']).exists())

    def test_cleanup_refuses_ignored_files_and_unmerged_refs_in_nested_submodules(self):
        base, _ = self.initialize()
        directory, task, _ = self.review(base)
        leaf = self.workspace / self.path / 'nested'
        (leaf / 'ignored').write_text('unpreserved artifact\n')
        with patch.object(dsh_task, 'owned'), \
                patch.object(dsh_task, 'docker_list', return_value=['fixture_workspace']), \
                patch.object(dsh_task, 'audit', side_effect=lambda box: audit(self.workspace)), \
                patch.object(dsh_task.dsh, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'changed since export'):
                dsh_task.cleanup(directory, task, Mock())
            task['reviews'][0]['audit'] = audit(self.workspace)
            with self.assertRaisesRegex(ValueError, 'Ignored files'):
                dsh_task.cleanup(directory, task, Mock())
            (leaf / 'ignored').unlink()
            head = git(leaf, 'rev-parse', 'HEAD')
            git(leaf, 'checkout', '-b', 'unmerged')
            (leaf / 'file').write_text('unmerged work\n')
            git(leaf, 'commit', '-am', 'unmerged work')
            git(leaf, 'checkout', '--detach', head)
            task['reviews'][0]['audit'] = audit(self.workspace)
            with self.assertRaisesRegex(ValueError, 'unpreserved refs'):
                dsh_task.cleanup(directory, task, Mock())
        run.assert_not_called()

    def test_refresh_preserves_submodule_archives_and_rejects_nested_ignored_files(self):
        _, bundle = self.initialize()
        leaf = self.workspace / self.path / 'nested'
        (leaf / 'ignored').write_text('cache\n')
        with self.assertRaisesRegex(ValueError, 'ignored files'):
            apply(self.workspace, bundle, False)
        (leaf / 'ignored').unlink()

        apply(self.workspace, bundle, False)

        self.assertTrue(git(leaf, 'for-each-ref', '--format=%(refname)', 'refs/heads/dsh/archive-*'))
        self.assertEqual(git(self.workspace, 'status', '--porcelain'), '')

    def test_finalize_preserves_nested_ignored_host_files(self):
        base, _ = self.initialize()
        leaf = self.workspace / self.path / 'nested'
        (leaf / 'ignored').write_text('agent tracked file\n')
        git(leaf, 'add', '-f', 'ignored')
        git(leaf, 'commit', '-m', 'track artifact')
        directory, task, report = self.review(base)
        dsh_task.prepare(directory, task)
        (self.leaf / 'ignored').write_text('valuable host file\n')

        with self.assertRaisesRegex(ValueError, 'ignored host file'):
            dsh_task.finalize(directory, task, report)

        self.assertEqual((self.leaf / 'ignored').read_text(), 'valuable host file\n')
        self.assertEqual(git(self.host, 'rev-parse', 'HEAD'), self.head)

    def test_added_submodule_is_integrated_without_network_fetches(self):
        base, _ = self.initialize()
        git(self.workspace, '-c', 'protocol.file.allow=always', 'submodule', 'add',
            str(self.root / 'leaf-source'), 'new-module')
        directory, task, report = self.review(base)

        dsh_task.prepare(directory, task)
        dsh_task.finalize(directory, task, report)

        self.assertEqual((self.host / 'new-module/file').read_text(), 'base\n')
        self.assertEqual(git(self.host, 'status', '--porcelain', '--ignore-submodules=none'), '')

    def test_removed_submodule_history_remains_in_bundle_and_preparation(self):
        base, _ = self.initialize()
        git(self.workspace, 'rm', '-f', '--', self.path)
        directory, task, _ = self.review(base)

        prepared = dsh_task.prepare(directory, task)

        self.assertNotIn(self.path, gitlinks(self.host, prepared['revision']))
        refs = module_refs(bundle_refs(self.host, task['reviews'][0]['bundle'], {'base', 'host', 'work'}))
        self.assertIn('base', refs[self.path])
        self.assertNotIn('work', refs[self.path])
        self.assertTrue((self.child / 'file').exists())

    def test_missing_submodules_fail_before_task_records_or_docker_resources(self):
        source = self.root / 'fresh-clone'
        git(self.host, 'clone', '--no-local', str(self.host), str(source))
        tasks = self.root / 'new-task-records'
        with patch.object(dsh_task, 'state_root', return_value=tasks), \
                patch.object(dsh_task, 'sandbox') as sandbox, \
                patch.object(dsh_task.dsh, 'run') as run:
            with self.assertRaisesRegex((ValueError, RuntimeError), 'submodule update|valid object'):
                dsh_task.main(['start', '--repo', str(source), '--task', 'missing'])
        sandbox.assert_not_called()
        run.assert_not_called()
        self.assertFalse(tasks.exists())

    def test_cleanup_preserves_unexported_commits_in_removed_submodule_metadata(self):
        base, _ = self.initialize()
        child = self.workspace / self.path
        (child / 'file').write_text('commit that must not disappear\n')
        git(child, 'commit', '-am', 'work before removing the module')
        git(self.workspace, 'rm', '-f', '--', self.path)
        directory, task, _ = self.review(base)
        state = audit(self.workspace)
        self.assertTrue(state['submodules'][self.path]['retired'])

        with patch.object(dsh_task, 'owned'), \
                patch.object(dsh_task, 'docker_list', return_value=['fixture_workspace']), \
                patch.object(dsh_task, 'audit', side_effect=lambda box: audit(self.workspace)), \
                patch.object(dsh_task.dsh, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'unpreserved refs'):
                dsh_task.cleanup(directory, task, Mock())
        run.assert_not_called()

    def test_unrecorded_removed_submodule_cache_is_not_silently_discarded(self):
        self.initialize()
        git(self.workspace, '-c', 'protocol.file.allow=always', 'submodule', 'add',
            str(self.root / 'leaf-source'), 'temporary-module')
        git(self.workspace, 'rm', '-f', '--', 'temporary-module')

        with self.assertRaisesRegex(ValueError, 'Unrecorded submodule repository'):
            audit(self.workspace)

    def test_deinitialized_local_submodule_objects_transfer_and_integrate_main_changes(self):
        git(self.host, 'submodule', 'deinit', '--', self.path)
        base, _ = self.initialize(snapshot=True, untracked=True)
        (self.workspace / 'agent-main').write_text('main repository work\n')
        directory, task, report = self.review(base)

        prepared = dsh_task.prepare(directory, task)
        dsh_task.finalize(directory, task, report)

        self.assertFalse((self.child / '.git').exists())
        self.assertFalse((Path(prepared['worktree']) / self.path / '.git').exists())
        self.assertEqual((self.host / 'agent-main').read_text(), 'main repository work\n')
        self.assertEqual(git(self.host, 'status', '--porcelain', '--ignore-submodules=none'), '')
        self.assertEqual((self.workspace / self.path / 'nested/file').read_text(), 'base\n')

    def test_git_tree_reads_non_utf8_file_names_without_losing_bytes(self):
        blob = git(self.leaf, 'hash-object', '-w', '--stdin', input='opaque file contents')
        entries = git(self.leaf, 'ls-tree', '-z', 'HEAD')
        tree = git(self.leaf, 'mktree', '-z', input=entries + f'100644 blob {blob}\tbad\udcff-name\0')

        listing = git(self.leaf, 'ls-tree', '-z', tree)

        self.assertIn(b'bad\xff-name\0', listing.encode('utf-8', 'surrogateescape'))
        self.assertEqual(gitlinks(self.leaf, tree), {})


if __name__ == '__main__':
    unittest.main()
