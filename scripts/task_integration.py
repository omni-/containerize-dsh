"""Apply agent-only changes in disposable integration checkouts, recursively."""
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess

from git_transfer import (git, gitlinks, module_names, subrepo, collect_modules,
                          fetch_objects, restore_modules, preservation_ref)


def repositories(repo):
    yield '', Path(repo)
    for path in gitlinks(repo, 'HEAD'):
        target = Path(repo) / path
        if not (target / '.git').exists():
            if not target.resolve().is_relative_to(Path(repo).resolve()):
                raise ValueError(f'Submodule path escaped the repository: {path}')
            if target.exists() and any(target.iterdir()):
                raise ValueError(f'Uninitialized submodule contains unpreserved files: {path}')
            continue
        module = subrepo(repo, path, 'HEAD')
        for name, child in repositories(module):
            yield path + ('/' + name if name else ''), child


def dirty(repo, ignored=False):
    options = ['--porcelain', '--ignore-submodules=none'] + (['--ignored'] if ignored else [])
    return '\n'.join((path + ': ' if path else '') + status
                     for path, child in repositories(repo)
                     if (status := git(child, 'status', *options)))


def prepare_modules(source, destination, target, refs, branch):
    # Gitlinks do not keep their target objects reachable in the parent repository.
    for key, (module, oid) in collect_modules(source, dict(target=target)).items():
        fetch_objects(source, module, oid)
        git(source, 'update-ref', preservation_ref(branch, key), oid)
    before = gitlinks(source, refs['base'][0])
    after = gitlinks(source, refs['work'][0])
    changed = {path for path in gitlinks(source, target) if before.get(path) != after.get(path)}
    restore_modules(source, destination, target, refs, paths=changed)


def apply_changes(source, destination, base, work, message):
    before, after = gitlinks(source, base), gitlinks(source, work)
    current = gitlinks(destination, 'HEAD')
    exclude = [':(top,literal,exclude)' + path for path in before.keys() | after.keys()]
    delta = subprocess.run(['git', '-C', str(source), 'diff', '--binary', '--full-index',
                            '--no-ext-diff', '--no-textconv', base, work, '--', '.', *exclude],
                           check=True, capture_output=True).stdout
    if delta:
        result = subprocess.run(['git', '-C', str(destination), 'apply', '--index', '--binary', '-'],
                                input=delta, capture_output=True)
        if result.returncode:
            raise ValueError(result.stderr.decode(errors='replace').strip())
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        child = Path(destination) / path
        if path not in before:
            if path in current:
                raise ValueError(f'Submodule was also added on the target: {path}')
            restore_modules(source, destination, work, {}, paths={path})
            oid = after[path]
        elif path not in after:
            if current.get(path) != before[path]:
                raise ValueError(f'Submodule removal conflicts with the target: {path}')
            resolved = child.resolve()
            if (not resolved.is_relative_to(Path(destination).resolve())
                    or not (resolved / '.git').is_dir()):
                raise ValueError(f'Submodule is outside the prepared checkout: {path}')
            # This is a fresh disposable copy; its history is pinned in the host.
            def retry_writable(operation, path, error):
                if not isinstance(error, PermissionError):
                    raise error
                os.chmod(path, 0o700)
                operation(path)
            shutil.rmtree(resolved, onexc=retry_writable)
            git(destination, 'update-index', '--force-remove', '--', path)
            continue
        else:
            if path not in current:
                raise ValueError(f'Submodule no longer exists on the target: {path}')
            fetch_objects(child, source, before[path], after[path])
            oid = apply_changes(source, child, before[path], after[path], message)
        git(destination, 'update-index', '--add', '--cacheinfo', '160000', oid, path)
    if git(destination, 'write-tree') != git(destination, 'rev-parse', 'HEAD^{tree}'):
        git(destination, '-c', 'core.hooksPath=' + os.devnull, 'commit', '-m', message)
    return git(destination, 'rev-parse', 'HEAD')


def install_objects(host, prepared, revision):
    """Load submodule histories locally before an offline host checkout update."""
    names = module_names(prepared, revision)
    for path, oid in gitlinks(prepared, revision).items():
        source = Path(prepared) / path
        if not (source / '.git').exists():
            # Unchanged dependencies need objects, not a host working checkout.
            git(prepared, 'cat-file', '-e', oid + '^{commit}')
            source = Path(prepared)
        target = Path(host) / path
        if not target.resolve().is_relative_to(Path(host).resolve()):
            raise ValueError(f'Submodule path escaped the host repository: {path}')
        metadata = not (target / '.git').exists()
        if not metadata:
            target = subrepo(host, path, 'HEAD')
            ignored = git(target, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard')
            tracked = git(source, 'ls-tree', '-r', '-z', '--name-only', oid).split('\0')
            tracked_paths = {name for name in tracked if name}
            parents = {str(parent) for name in tracked_paths for parent in PurePosixPath(name).parents}
            for name in ignored.split('\0'):
                name = name.rstrip('/')
                if name and (name in tracked_paths or name in parents
                             or any(str(parent) in tracked_paths for parent in PurePosixPath(name).parents)):
                    raise ValueError(f'Submodule update would overwrite an ignored host file: {path}/{name}')
        else:
            if target.exists() and any(target.iterdir()):
                raise ValueError(f'Submodule destination contains host files: {target}')
            target = Path(git(host, 'rev-parse', '--path-format=absolute', '--git-path',
                              'modules/' + names[path]))
            if not target.exists():
                target.mkdir(parents=True)
                git(target, 'init', '--bare', '--object-format=' + git(source, 'rev-parse', '--show-object-format'))
        fetch_objects(target, source, oid)
        git(target, 'update-ref', 'refs/dsh/integrated/' + oid, oid)
        install_objects(target, source, oid)
        if metadata:
            git(target, 'config', 'core.bare', 'false')


def update_checkouts(host, source, revision, previous):
    before = gitlinks(source, previous) if previous else {}
    for path, oid in gitlinks(source, revision).items():
        target = Path(host) / path
        if not (target / '.git').exists() and path in before:
            continue
        git(host, '-c', 'core.hooksPath=' + os.devnull, 'submodule', 'update',
            '--init', '--no-fetch', '--checkout', '--', path)
        update_checkouts(target, source, oid, before.get(path))
