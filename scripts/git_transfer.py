"""Git-native transfers; no shell evaluation and no access to the user's index."""
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


def git(repo, *args, env=None, input=None):
    result = subprocess.run(
        ['git', '-C', str(repo), *map(str, args)], env=env, input=input,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def commit(repo, revision='HEAD', snapshot=False, untracked=False):
    head = git(repo, 'rev-parse', '--verify', f'{revision}^{{commit}}')
    if not snapshot:
        return head, head
    if revision != 'HEAD':
        raise ValueError('A snapshot uses HEAD; do not combine --snapshot and --revision.')
    if git(repo, 'ls-files', '--stage').find('160000 ') >= 0:
        raise ValueError('Snapshot transfer does not include submodule worktrees. Use a revision.')
    with tempfile.TemporaryDirectory(prefix='dsh-index-') as directory:
        env = os.environ.copy()
        env['GIT_INDEX_FILE'] = str(Path(directory) / 'index')
        env.update(GIT_AUTHOR_NAME='DSH snapshot', GIT_AUTHOR_EMAIL='snapshot@localhost',
                   GIT_COMMITTER_NAME='DSH snapshot', GIT_COMMITTER_EMAIL='snapshot@localhost')
        git(repo, 'read-tree', head, env=env)
        git(repo, 'add', '-A' if untracked else '-u', '--', '.', env=env)
        tree = git(repo, 'write-tree', env=env)
        if tree == git(repo, 'rev-parse', f'{head}^{{tree}}'):
            return head, head
        return git(repo, 'commit-tree', tree, '-p', head, env=env,
                   input='DSH working-tree snapshot\n'), head


def make_bundle(repo, output, refs):
    """Temporary refs make synthetic commits reachable while bundling."""
    output = Path(output).resolve()
    if output.exists():
        raise ValueError(f'Refusing to overwrite {output}')
    prefix = f'refs/dsh-transfer/{uuid.uuid4().hex}'
    created = []
    try:
        for name, oid in refs.items():
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
        name = ref.rsplit('/', 1)[-1]
        if name in refs or name not in expected or not ref.startswith('refs/dsh-transfer/'):
            raise ValueError('Unexpected bundle refs')
        refs[name] = (oid, ref)
    if set(refs) != set(expected):
        raise ValueError('Missing bundle refs')
    return refs
