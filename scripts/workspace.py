"""Runs inside a stopped sandbox's short-lived maintenance container."""
import argparse
import json
from pathlib import Path
import uuid
from git_transfer import git, commit, make_bundle, bundle_refs


def apply(repo, bundle, initialize):
    if initialize:
        if any(repo.iterdir()):
            raise ValueError('Workspace is not empty; initialization refused.')
        git(repo, 'init', '--initial-branch=dsh-empty')
    elif not (repo / '.git').is_dir():
        raise ValueError('Workspace is not an independent Git repository.')
    else:
        if git(repo, 'status', '--porcelain', '--ignored'):
            raise ValueError('Workspace has dirty, untracked, or ignored files. Export/archive them first.')
        git(repo, 'branch', 'dsh/archive-' + uuid.uuid4().hex, 'HEAD')
    refs = bundle_refs(repo, bundle, {'base', 'host'})
    git(repo, '-c', 'core.hooksPath=/dev/null', 'fetch', '--no-tags', bundle,
        *[value[1] for value in refs.values()])
    git(repo, '-c', 'core.hooksPath=/dev/null', 'checkout', '-b',
        'dsh/work-' + uuid.uuid4().hex, refs['base'][0])
    git(repo, 'update-ref', 'refs/dsh/base', refs['base'][0])
    git(repo, 'update-ref', 'refs/dsh/host', refs['host'][0])
    git(repo, 'config', 'user.name', 'DSH agent')
    git(repo, 'config', 'user.email', 'agent@localhost')


def status(repo):
    base = git(repo, 'rev-parse', 'refs/dsh/base')
    return dict(base=base, host=git(repo, 'rev-parse', 'refs/dsh/host'),
                head=git(repo, 'rev-parse', 'HEAD'),
                divergence=git(repo, 'rev-list', '--left-right', '--count', f'{base}...HEAD'),
                changes=git(repo, 'status', '--short'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'refresh', 'export', 'status'])
    parser.add_argument('--bundle')
    parser.add_argument('--snapshot', action='store_true')
    parser.add_argument('--include-untracked', action='store_true')
    args = parser.parse_args()
    repo = Path('/workspace')
    if args.action in ('init', 'refresh'):
        apply(repo, args.bundle, args.action == 'init')
    elif args.action == 'export':
        if not args.snapshot and git(repo, 'status', '--porcelain'):
            raise ValueError('Commit work or use --snapshot to export dirty work.')
        work, _ = commit(repo, snapshot=args.snapshot, untracked=args.include_untracked)
        base = git(repo, 'rev-parse', 'refs/dsh/base')
        git(repo, 'merge-base', '--is-ancestor', base, work)
        make_bundle(repo, args.bundle, dict(work=work, base=base,
                    host=git(repo, 'rev-parse', 'refs/dsh/host')))
    else:
        print(json.dumps(status(repo)))


if __name__ == '__main__':
    main()
