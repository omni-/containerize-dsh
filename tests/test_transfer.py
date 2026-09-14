import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from git_transfer import git, commit, make_bundle, bundle_refs


class TransferTests(unittest.TestCase):
    def test_dirty_linked_worktree_snapshot_and_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / 'host repo'
            repo.mkdir()
            git(repo, 'init')
            git(repo, 'config', 'user.name', 'Test')
            git(repo, 'config', 'user.email', 'test@localhost')
            (repo / 'tracked').write_text('original')
            (repo / '.gitignore').write_text('secret\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'base')
            work = root / 'linked checkout'
            git(repo, 'worktree', 'add', '-b', 'fixture', work)
            (work / 'tracked').write_text('staged')
            git(work, 'add', 'tracked')
            (work / 'tracked').write_text('unstaged')
            (work / 'new').write_text('new file')
            (work / 'secret').write_text('must not transfer')
            index = Path(git(work, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
            before = index.read_bytes()
            head = git(work, 'rev-parse', 'HEAD')
            tracked_only, _ = commit(work, snapshot=True)
            self.assertNotIn('new', git(work, 'ls-tree', '--name-only', tracked_only).splitlines())
            snapshot, source = commit(work, snapshot=True, untracked=True)
            self.assertEqual(source, head)
            self.assertEqual(git(work, 'show', f'{snapshot}:tracked'), 'unstaged')
            self.assertEqual(git(work, 'show', f'{snapshot}:new'), 'new file')
            self.assertNotIn('secret', git(work, 'ls-tree', '--name-only', snapshot))
            self.assertEqual(index.read_bytes(), before)
            self.assertEqual(git(work, 'rev-parse', 'HEAD'), head)
            self.assertEqual((work / 'tracked').read_text(), 'unstaged')
            bundle = root / 'snapshot.bundle'
            make_bundle(work, bundle, dict(base=snapshot, host=head))
            target = root / 'other machine'
            target.mkdir()
            git(target, 'init')
            refs = bundle_refs(target, bundle, {'base', 'host'})
            git(target, 'fetch', bundle, refs['base'][1])
            git(target, 'checkout', '-b', 'review', snapshot)
            self.assertEqual((target / 'tracked').read_text(), 'unstaged')
            self.assertFalse((target / 'secret').exists())
            self.assertEqual(git(work, 'for-each-ref', 'refs/dsh-transfer'), '')
            with self.assertRaises(ValueError):
                make_bundle(work, bundle, dict(base=head))


if __name__ == '__main__':
    unittest.main()
