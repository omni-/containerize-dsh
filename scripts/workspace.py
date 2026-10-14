"""Runs inside a stopped sandbox's short-lived maintenance container."""
import argparse
import json
import os
from pathlib import Path
import uuid
from git_transfer import (git, commit, make_bundle, bundle_refs, fetch_objects,
                          gitlinks, subrepo, module_refs, validate_modules, restore_modules)


def apply(repo, bundle, initialize):
    if initialize:
        if any(repo.iterdir()):
            raise ValueError('Workspace is not empty; initialization refused.')
        git(repo, 'init', '--initial-branch=dsh-empty')
    elif not (repo / '.git').is_dir():
        raise ValueError('Workspace is not an independent Git repository.')
    else:
        current = audit(repo)
        if git(repo, 'status', '--porcelain', '--ignored', '--ignore-submodules=none') or current['ignored']:
            raise ValueError('Workspace has dirty, untracked, or ignored files. Export/archive them first.')
    refs = bundle_refs(repo, bundle, {'base', 'host'})
    fetch_objects(repo, bundle, *[value[1] for value in refs.values()])
    validate_modules(repo, refs)
    if not initialize:
        incoming = {path for path, values in module_refs(refs).items() if 'base' in values}
        if not current.get('submodules', {}).keys() <= incoming:
            raise ValueError('Refresh would remove submodule repositories. Export/archive them first.')
        git(repo, 'branch', 'dsh/archive-' + uuid.uuid4().hex, 'HEAD')
    git(repo, '-c', 'core.hooksPath=/dev/null', 'checkout', '-b',
        'dsh/work-' + uuid.uuid4().hex, refs['base'][0])
    git(repo, 'update-ref', 'refs/dsh/base', refs['base'][0])
    git(repo, 'update-ref', 'refs/dsh/host', refs['host'][0])
    git(repo, 'config', 'user.name', 'DSH agent')
    git(repo, 'config', 'user.email', 'agent@localhost')
    restore_modules(repo, repo, refs['base'][0], refs)


def audit(repo, work=None, transferred=True, retired=False):
    """Compare content and preservation state recursively, including ignored files."""
    if work is None:
        if retired:
            work = git(repo, 'rev-parse', 'HEAD')
        else:
            work, _ = commit(repo, snapshot=True, untracked=True)
    head = git(repo, 'rev-parse', 'HEAD')
    origins = {}
    for label in ('base', 'host'):
        try:
            origins[label] = git(repo, 'rev-parse', '--verify', 'refs/dsh/' + label)
        except RuntimeError:
            if transferred:
                raise
            origins[label] = head
    state = dict(**origins, head=head, gitdir=git(repo, 'rev-parse', '--absolute-git-dir'),
                 tree=git(repo, 'rev-parse', work + '^{tree}'), index=git(repo, 'write-tree'),
                 new='' if retired else git(repo, 'diff', '--name-only', '--diff-filter=A', 'HEAD', work),
                 ignored='' if retired else git(repo, 'ls-files', '--others', '--ignored', '--exclude-standard'),
                 refs=git(repo, 'for-each-ref', '--format=%(objectname)').splitlines())
    if retired:
        state['retired'] = True
        state['worktree'] = (Path(repo) / '.git').exists()
    modules = {}
    links = gitlinks(repo, work)
    known = {}
    for revision in (state['base'], state['host'], head):
        for path, oid in gitlinks(repo, revision).items():
            known.setdefault(path, (oid, revision))
    known.update({path: (oid, work) for path, oid in links.items()})
    for path, (oid, revision) in known.items():
        removed = retired or path not in links
        try:
            module = subrepo(repo, path, revision)
        except ValueError:
            if removed:
                continue  # No repository or working files remain to be deleted.
            raise
        if not removed and not (module / '.git').exists():
            raise ValueError(f'Submodule {path} is not initialized. Restore it before export or cleanup.')
        child = audit(module, None if removed else oid, transferred=False, retired=removed)
        descendants = child.pop('submodules', {})
        modules[path] = child
        modules.update({path + '/' + name: value for name, value in descendants.items()})
        for key in ('new', 'ignored'):
            extra = '\n'.join(path + '/' + name for name in child[key].splitlines())
            state[key] = '\n'.join(value for value in (state[key], extra) if value)
    if modules:
        state['submodules'] = modules
    cache = Path(state['gitdir']) / 'modules'
    known_dirs = {Path(child['gitdir']).resolve() for child in modules.values()}
    def inaccessible(error):
        raise error
    for directory, children, files in (os.walk(cache, onerror=inaccessible) if cache.is_dir() else ()):
        if 'HEAD' in files and 'objects' in children:
            if Path(directory).resolve() not in known_dirs:
                raise ValueError(f'Unrecorded submodule repository needs separate preservation: {directory}')
            children[:] = [name for name in children if name == 'modules']
    return state


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
        if not args.snapshot and git(repo, 'status', '--porcelain', '--ignore-submodules=none'):
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
