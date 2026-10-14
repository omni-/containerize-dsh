"""Git-native transfers; no shell evaluation and no access to the user's index."""
import os
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import uuid


def git(repo, *args, env=None, input=None):
    command = ['git', '-c', 'core.longpaths=true', '-C', str(repo)]
    directory = Path(repo)
    if (not (directory / '.git').exists() and (directory / 'HEAD').is_file()
            and (directory / 'objects').is_dir()):
        # Absorbed submodule metadata can refer to a checkout that was removed.
        command += ['--git-dir=' + str(directory.resolve()), '--work-tree=' + str(directory.resolve())]
    result = subprocess.run(
        [*command, *map(str, args)], env=env, input=input,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding='utf-8', errors='surrogateescape',
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def safe_path(path):
    """Validate paths before using repository or bundle metadata on disk."""
    parts = PurePosixPath(path).parts
    if (not parts or path != '/'.join(parts) or any(part in ('.', '..') for part in parts)
            or any(part.lower() == '.git' for part in parts) or '\\' in path or ':' in path
            or '\0' in path or PurePosixPath(path).is_absolute()):
        raise ValueError(f'Unsafe submodule path: {path!r}')
    return path


def gitlinks(repo, revision):
    links = {}
    for entry in git(repo, 'ls-tree', '-r', '-z', revision).split('\0'):
        if entry.startswith('160000 '):
            metadata, path = entry.split('\t', 1)
            links[safe_path(path)] = metadata.split()[2]
    return links


def module_names(repo, revision):
    if not gitlinks(repo, revision):
        return {}
    try:
        entries = git(repo, 'config', '--null', '--blob', f'{revision}:.gitmodules',
                      '--get-regexp', r'^submodule\..*\.path$')
    except RuntimeError as error:
        raise ValueError('Nested repositories must be registered in .gitmodules before transfer') from error
    names = {}
    for entry in entries.split('\0'):
        if entry:
            key, path = entry.split('\n', 1)
            name = safe_path(key[len('submodule.'):-len('.path')])
            path = safe_path(path)
            if path in names:
                raise ValueError(f'Duplicate submodule path: {path}')
            names[path] = name
    if not gitlinks(repo, revision).keys() <= names.keys():
        raise ValueError('Nested repositories must be registered in .gitmodules before transfer')
    return names


def subrepo(repo, path, revision):
    """Find only local submodule objects; never clone a URL from .gitmodules."""
    path = safe_path(path)
    root = Path(repo).resolve()
    work = (root / path).resolve()
    if not work.is_relative_to(root) or work == root:
        raise ValueError(f'Submodule path escaped the repository: {path}')
    if (work / '.git').exists():
        if Path(git(work, 'rev-parse', '--show-toplevel')).resolve() != work:
            raise ValueError(f'Submodule is not an independent repository: {path}')
        return work
    name = module_names(repo, revision)[path]
    directory = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-path', 'modules/' + name))
    if directory.is_dir():
        return directory
    raise ValueError(f'Submodule {path} is unavailable locally. Run git submodule update --init --recursive first.')


def commit(repo, revision='HEAD', snapshot=False, untracked=False):
    head = git(repo, 'rev-parse', '--verify', f'{revision}^{{commit}}')
    if not snapshot:
        return head, head
    if revision != 'HEAD':
        raise ValueError('A snapshot uses HEAD; do not combine --snapshot and --revision.')
    with tempfile.TemporaryDirectory(prefix='dsh-index-') as directory:
        env = os.environ.copy()
        env['GIT_INDEX_FILE'] = str(Path(directory) / 'index')
        # Parent gitlinks must be stable across repeated audits of unchanged edits.
        timestamp = git(repo, 'show', '-s', '--format=%ct', head) + ' +0000'
        env.update(GIT_AUTHOR_NAME='DSH snapshot', GIT_AUTHOR_EMAIL='snapshot@localhost',
                   GIT_COMMITTER_NAME='DSH snapshot', GIT_COMMITTER_EMAIL='snapshot@localhost',
                   GIT_AUTHOR_DATE=timestamp, GIT_COMMITTER_DATE=timestamp)
        git(repo, 'read-tree', head, env=env)
        git(repo, 'add', '-A' if untracked else '-u', '--', '.', env=env)
        tree = git(repo, 'write-tree', env=env)
        module_names(repo, tree)
        for path, oid in gitlinks(repo, tree).items():
            module = subrepo(repo, path, tree)
            if (module / '.git').exists():
                oid, _ = commit(module, snapshot=True, untracked=untracked)
            git(repo, 'update-index', '--add', '--cacheinfo', '160000', oid, path, env=env)
        tree = git(repo, 'write-tree', env=env)
        if tree == git(repo, 'rev-parse', f'{head}^{{tree}}'):
            return head, head
        return git(repo, 'commit-tree', tree, '-p', head, env=env,
                   input='DSH working-tree snapshot\n'), head


def module_key(path, kind):
    encoded = safe_path(path).encode('utf-8').hex()
    return 'modules/' + '/'.join(encoded[index:index + 128] for index in range(0, len(encoded), 128)) + '/' + kind


def preservation_ref(branch, key):
    namespace = hashlib.sha256(branch.encode('utf-8')).hexdigest()[:24]
    return 'refs/dsh-submodules/' + namespace + '/' + key


def module_refs(refs):
    modules = {}
    for key, value in refs.items():
        if key.startswith('modules/'):
            parts = key.split('/')
            encoded, kind = ''.join(parts[1:-1]), parts[-1]
            try:
                path = safe_path(bytes.fromhex(encoded).decode('utf-8'))
            except (ValueError, UnicodeError) as error:
                raise ValueError('Invalid submodule bundle ref') from error
            if module_key(path, kind) != key:
                raise ValueError('Invalid submodule bundle ref')
            modules.setdefault(path, {})[kind] = value
    return modules


def collect_modules(repo, refs):
    modules = {}

    def visit(source, oid, kind, prefix='', ancestors=()):
        if oid in ancestors:
            raise ValueError('Cyclic submodule repositories are not supported')
        for path, child in gitlinks(source, oid).items():
            try:
                module = subrepo(source, path, oid)
            except ValueError as unavailable:
                # Removed submodules can still be preserved by an earlier transfer.
                try:
                    git(repo, 'cat-file', '-e', child + '^{commit}')
                except RuntimeError:
                    raise unavailable
                module = Path(repo)
            full = prefix + path
            git(module, 'cat-file', '-e', child + '^{commit}')
            modules[module_key(full, kind)] = (module, child)
            visit(module, child, kind, full + '/', (*ancestors, oid))

    for kind, oid in refs.items():
        visit(repo, oid, kind)
    return modules


def fetch_objects(repo, source, *refs):
    git(repo, '-c', 'core.hooksPath=' + os.devnull, '-c', 'fetch.recurseSubmodules=false',
        'fetch', '--no-tags', '--no-write-fetch-head', str(source), *refs)


def make_bundle(repo, output, refs):
    """Temporary refs make synthetic commits reachable while bundling."""
    output = Path(output).resolve()
    if output.exists():
        raise ValueError(f'Refusing to overwrite {output}')
    prefix = f'refs/dsh-transfer/{uuid.uuid4().hex}'
    created = []
    try:
        all_refs = dict(refs)
        for name, (module, oid) in collect_modules(repo, refs).items():
            if Path(module).resolve() != Path(repo).resolve():
                fetch_objects(repo, module, oid)
            all_refs[name] = oid
        for name, oid in all_refs.items():
            ref = f'{prefix}/{name}'
            git(repo, 'update-ref', ref, oid, '0' * len(oid))
            created.append(ref)
        git(repo, 'bundle', 'create', output, *created)
    finally:
        for ref in created:
            git(repo, 'update-ref', '-d', ref)


def bundle_refs(repo, bundle, expected):
    git(repo, 'bundle', 'verify', bundle)
    refs = {}
    for line in git(repo, 'bundle', 'list-heads', bundle).splitlines():
        oid, ref = line.split()
        match = re.fullmatch(r'refs/dsh-transfer/([0-9a-f]{32})/(.+)', ref)
        name = match[2] if match else ''
        module = re.fullmatch(r'modules/(?:[0-9a-f]+/)+([^/]+)', name)
        if (name in refs or not match or (name not in expected and not module)
                or (module and module[1] not in expected)):
            raise ValueError('Unexpected bundle refs')
        refs[name] = (oid, ref)
    if {key for key in refs if not key.startswith('modules/')} != set(expected):
        raise ValueError('Missing bundle refs')
    module_refs(refs)
    return refs


def validate_modules(repo, refs):
    """Verify every payload is pinned by the corresponding repository tree."""
    expected = {}

    def visit(oid, kind, prefix='', ancestors=()):
        if oid in ancestors:
            raise ValueError('Cyclic submodule repositories are not supported')
        module_names(repo, oid)
        for path, child in gitlinks(repo, oid).items():
            full = prefix + path
            key = module_key(full, kind)
            if key not in refs or refs[key][0] != child:
                raise ValueError(f'Missing or mismatched submodule payload: {full} ({kind})')
            expected[key] = child
            visit(child, kind, full + '/', (*ancestors, oid))

    for kind, (oid, _) in refs.items():
        if not kind.startswith('modules/'):
            visit(oid, kind)
    if {key for key in refs if key.startswith('modules/')} != expected.keys():
        raise ValueError('Unexpected submodule payload')


def restore_modules(source, destination, revision, refs, prefix='', paths=None):
    """Populate independent submodule checkouts from local transferred objects."""
    for path, oid in gitlinks(source, revision).items():
        if paths is not None and path not in paths:
            continue
        target = Path(destination) / path
        resolved = target.resolve()
        if not resolved.is_relative_to(Path(destination).resolve()):
            raise ValueError(f'Submodule path escaped the repository: {path}')
        if (target / '.git').exists():
            if git(target, 'status', '--porcelain', '--ignored', '--ignore-submodules=none'):
                raise ValueError(f'Submodule has unpreserved files: {target}')
            git(target, 'branch', 'dsh/archive-' + uuid.uuid4().hex, 'HEAD')
        else:
            if target.exists() and any(target.iterdir()):
                raise ValueError(f'Submodule destination is not empty: {target}')
            target.mkdir(parents=True, exist_ok=True)
            git(target, 'init', '--initial-branch=dsh-empty',
                '--object-format=' + git(source, 'rev-parse', '--show-object-format'))
        full = prefix + path
        values = module_refs(refs).get(full, {})
        fetch_objects(target, source, oid, *dict.fromkeys(value[0] for value in values.values()))
        git(target, '-c', 'core.hooksPath=' + os.devnull, 'checkout', '-b',
            'dsh/work-' + uuid.uuid4().hex, oid)
        for label in ('base', 'host'):
            git(target, 'update-ref', 'refs/dsh/' + label, values.get(label, (oid, ''))[0])
        git(target, 'config', 'user.name', 'DSH agent')
        git(target, 'config', 'user.email', 'agent@localhost')
        restore_modules(source, target, oid, refs, full + '/')
