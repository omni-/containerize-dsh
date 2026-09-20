#!/usr/bin/env python3
"""Host task bookkeeping around dsh.py; no host checkout is mounted in Docker."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
import webbrowser

import dsh
from git_transfer import git, bundle_refs


def state_root():
    return dsh.profiles_path().parent / 'tasks'


def save(directory, task):
    temporary = directory / 'manifest.tmp'
    temporary.write_text(json.dumps(task, indent=2) + '\n', encoding='utf-8')
    temporary.replace(directory / 'manifest.json')


@contextmanager
def locked(task_id):
    if not re.fullmatch(r'[a-z0-9-]+-[0-9a-f]{32}', task_id):
        raise ValueError('Invalid task ID')
    directory = state_root() / task_id
    with (directory / 'lock').open('x'):
        pass
    try:
        task = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
        if task['id'] != task_id:
            raise ValueError('Task identity mismatch')
        yield directory, task
    finally:
        (directory / 'lock').unlink()


def identity(repo):
    return git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')


def host(task):
    repo = Path(task['repo'])
    if identity(repo) != task['repository']:
        raise ValueError('Source repository identity changed')
    return repo


def args_for(task, action, **kwargs):
    return argparse.Namespace(**task['resolved'], action=action, **kwargs)


@contextmanager
def sandbox(task):
    with tempfile.TemporaryDirectory(prefix='dsh-task-compose-') as temp:
        box = dsh.Sandbox(args_for(task, 'status'), temp)
        config = json.loads(box.compose('config', '--format', 'json', capture=True))
        for kind, name in task['volumes'].items():
            volume = config['volumes'][kind]
            if volume['name'] != name or volume.get('external'):
                raise ValueError('Task requires its own nonexternal home/workspace volumes')
        yield box


def docker_list(*args):
    return dsh.run('docker', *args, capture=True).splitlines()


def owned(task, allow_missing=False):
    """Verify exact volume creation identity and all consumers before any stop/removal."""
    project_containers = docker_list('ps', '-aq', '--no-trunc', '--filter',
                                     'label=com.docker.compose.project=' + task['resolved']['name'])
    if any(cid != task.get('container') for cid in project_containers):
        raise ValueError('An unrecorded container occupies this task namespace')
    for kind, name in task['volumes'].items():
        names = docker_list('volume', 'ls', '--format', '{{.Name}}')
        if name not in names:
            if allow_missing and kind == 'workspace':
                continue
            raise ValueError(f'Missing task volume: {name}')
        info = json.loads(dsh.run('docker', 'volume', 'inspect', name, capture=True))[0]
        labels = info.get('Labels') or {}
        if (labels.get('com.docker.compose.project') != task['resolved']['name']
                or labels.get('com.docker.compose.volume') != kind
                or info.get('CreatedAt') != task['volume_created'][kind]):
            raise ValueError('Volume ownership changed')
        consumers = docker_list('ps', '-aq', '--filter', f'volume={name}')
        for cid in consumers:
            info = json.loads(dsh.run('docker', 'inspect', cid, capture=True))[0]
            if info['Id'] != task.get('container'):
                raise ValueError('An unrecorded container uses a task volume; refusing to change it')


# Run using the existing image helpers; old built images remain usable.
AUDIT = '''import sys,json
sys.path.insert(0,"/opt/dsh")
from git_transfer import git,commit
from pathlib import Path
r=Path("/workspace")
w,_=commit(r,snapshot=True,untracked=True)
if any(line.startswith("160000 ") for line in git(r,"ls-tree","-r",w).splitlines()):
    raise ValueError("Nested repositories/submodules need separate preservation before task export or cleanup")
print(json.dumps(dict(base=git(r,"rev-parse","refs/dsh/base"),
host=git(r,"rev-parse","refs/dsh/host"),head=git(r,"rev-parse","HEAD"),
tree=git(r,"rev-parse",w+"^{tree}"),index=git(r,"write-tree"),
new=git(r,"diff","--name-only","--diff-filter=A","HEAD",w),
ignored=git(r,"ls-files","--others","--ignored","--exclude-standard"),
refs=git(r,"for-each-ref","--format=%(objectname)").splitlines())))
'''


def audit(box):
    with box.maintenance() as container:
        return json.loads(dsh.run('docker', 'exec', container, 'python3', '-c', AUDIT, capture=True))


def export_task(directory, task, box, include_untracked=False):
    repo = host(task)
    owned(task)
    box.compose('stop')
    before = audit(box)
    if before['base'] != task['base'] or before['host'] != task['transfer_host']:
        raise ValueError('Workspace transfer identity changed')
    if before['new'] and not include_untracked:
        raise ValueError('New files require explicit --include-untracked:\n' + before['new'])
    number = uuid.uuid4().hex
    bundle = directory / f'review-{number}.bundle'
    dsh.transfer(box, args_for(task, 'export', bundle=str(bundle), snapshot=True,
                               include_untracked=include_untracked))
    refs = bundle_refs(repo, bundle, {'base', 'host', 'work'})
    branch = f'dsh/review-{task["id"]}-{number}'
    dsh.transfer(None, args_for(task, 'import', repo=str(repo), bundle=str(bundle), branch=branch))
    if audit(box) != before or git(repo, 'rev-parse', refs['work'][0] + '^{tree}') != before['tree']:
        raise ValueError(f'Workspace changed during export; bundle retained at {bundle}')
    review = dict(revision=refs['work'][0], branch=branch, bundle=str(bundle),
                  sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(), audit=before)
    task.setdefault('reviews', []).append(review)
    save(directory, task)
    return review


def latest(task):
    if not task.get('reviews'):
        raise ValueError('Export for review first')
    review = task['reviews'][-1]
    repo = host(task)
    bundle = Path(review['bundle'])
    if hashlib.sha256(bundle.read_bytes()).hexdigest() != review['sha256']:
        raise ValueError('Recovery bundle changed')
    refs = bundle_refs(repo, bundle, {'base', 'host', 'work'})
    if (refs['work'][0] != review['revision'] or refs['base'][0] != task['base']
            or refs['host'][0] != task['transfer_host']
            or git(repo, 'rev-parse', review['branch']) != review['revision']):
        raise ValueError('Review refs no longer match the recorded export')
    return review


def evidence(directory, path, prefix):
    content = Path(path).read_bytes()
    if not content.strip():
        raise ValueError('A nonempty report is required')
    target = directory / (prefix + '-' + uuid.uuid4().hex + '.txt')
    target.write_bytes(content)
    return str(target)


def prepare(directory, task):
    review = latest(task)
    if not review.get('report'):
        raise ValueError('Review this exact revision and record its report first')
    repo = host(task)
    target = git(repo, 'rev-parse', 'refs/heads/' + task['target'])
    suffix = uuid.uuid4().hex
    branch = 'dsh/integrate-' + task['id'] + '-' + suffix
    work = directory / ('integration-' + suffix)
    git(repo, 'worktree', 'add', '-b', branch, work, target)
    prepared = dict(branch=branch, worktree=str(work), target_head=target,
                    review=review['revision'], status='preparing')
    task.setdefault('integrations', []).append(prepared)
    save(directory, task)
    # Strict patch application deliberately refuses baseline-dependent conflicts.
    # Never cherry-pick the synthetic base or merge its ancestry into the target.
    patch = subprocess.run(['git', '-C', str(repo), 'diff', '--binary', '--full-index',
                            '--no-ext-diff', '--no-textconv', task['base'], review['revision']],
                           check=True, capture_output=True).stdout
    if patch:
        result = subprocess.run(['git', '-C', str(work), 'apply', '--index', '--binary', '-'],
                                input=patch, capture_output=True)
        if result.returncode:
            prepared['status'] = 'blocked'
            save(directory, task)
            raise ValueError('Agent-only patch conflicts or needs the dirty baseline. Prepared worktree: '
                             + str(work) + '\n' + result.stderr.decode(errors='replace'))
        git(work, '-c', 'core.hooksPath=' + os.devnull, 'commit', '-m', 'Integrate DSH task ' + task['id'])
    prepared['revision'] = git(work, 'rev-parse', 'HEAD')
    prepared['status'] = 'prepared'
    save(directory, task)
    return prepared


def finalize(directory, task, checks):
    review = latest(task)
    prepared = task['integrations'][-1]
    repo = host(task)
    work = Path(prepared['worktree'])
    if (prepared['status'] != 'prepared' or prepared['review'] != review['revision']
            or git(work, 'rev-parse', 'HEAD') != prepared['revision']
            or git(work, 'status', '--porcelain')):
        raise ValueError('Prepared revision changed; review and prepare again')
    # Use the original checkout only when it still owns the intended target.
    if (git(repo, 'symbolic-ref', '--short', 'HEAD') != task['target']
            or git(repo, 'rev-parse', 'HEAD') != prepared['target_head']
            or git(repo, 'status', '--porcelain')):
        raise ValueError('Target moved, is dirty, or is checked out elsewhere; prepared branch retained. Integration is incomplete.')
    report = evidence(directory, checks, 'checks')
    git(repo, '-c', 'core.hooksPath=' + os.devnull, 'merge', '--ff-only',
        '--no-autostash', '--no-overwrite-ignore', prepared['revision'])
    prepared.update(status='integrated', checks=report)
    task['integrated'] = dict(review=review['revision'], revision=prepared['revision'])
    save(directory, task)


def cleanup(directory, task, box, require_integrated=False):
    if task.get('cleaned'):
        return
    review = latest(task)
    if not review.get('report'):
        raise ValueError('Exact exported revision has not been reviewed')
    if require_integrated or task.get('integrations'):
        integrated = task.get('integrated', {})
        if integrated.get('review') != review['revision']:
            raise ValueError('Integration is incomplete for this revision')
        git(host(task), 'merge-base', '--is-ancestor', integrated['revision'], 'refs/heads/' + task['target'])
    removable = []
    for prepared in task.get('integrations', []):
        work = Path(prepared['worktree']).resolve()
        if not work.is_relative_to(directory.resolve()) or work == directory.resolve():
            raise ValueError('Integration worktree is outside task state')
        if work.exists():
            if (identity(work) != task['repository'] or git(work, 'status', '--porcelain', '--ignored')
                    or git(work, 'rev-parse', 'HEAD') != prepared.get('revision')):
                raise ValueError('Integration worktree has unpreserved changes; retain it: ' + str(work))
            removable.append(work)
    owned(task, allow_missing=task.get('cleanup_verified', False))
    names = docker_list('volume', 'ls', '--format', '{{.Name}}')
    if task['volumes']['workspace'] in names:
        box.compose('stop')
        current = audit(box)
        if current != review['audit']:
            raise ValueError('Workspace changed since export; review again')
        if current['ignored']:
            raise ValueError('Ignored files are not in the bundle; archive them outside the checkout before cleanup:\n' + current['ignored'])
        for oid in current['refs']:
            git(host(task), 'merge-base', '--is-ancestor', oid, review['revision'])
        task['cleanup_verified'] = True
        save(directory, task)
        # Remove only the recorded container. Preserve home, networks, images and caches.
        if task.get('container'):
            if task['container'] in docker_list('ps', '-aq', '--no-trunc'):
                dsh.run('docker', 'rm', task['container'])
            task['container'] = None
            save(directory, task)
        owned(task)
        dsh.run('docker', 'volume', 'rm', task['volumes']['workspace'])
    for work in removable:
        git(host(task), 'worktree', 'remove', work)
    task['cleaned'] = True
    save(directory, task)


def discard(directory, task):
    """Explicitly abandon container work; keep host worktrees, home and records."""
    if task.get('cleaned'):
        return True
    owned(task, allow_missing=task.get('discard_started', False))
    try:
        answer = input(f'Permanently discard task {task["id"]} and ALL workspace files '
                       '(including uncommitted, untracked and ignored files)? [y/n] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ''
    if answer != 'y':
        print('Discard cancelled; no resources changed.')
        return False
    task['discard_started'] = True
    save(directory, task)
    cid = task.get('container')
    if cid and cid in docker_list('ps', '-aq', '--no-trunc'):
        dsh.run('docker', 'stop', cid)
        # Recheck ownership after stopping, before removing anything.
        owned(task, allow_missing=True)
        dsh.run('docker', 'rm', cid)
    task['container'] = None
    save(directory, task)
    owned(task, allow_missing=True)
    workspace = task['volumes']['workspace']
    if workspace in docker_list('volume', 'ls', '--format', '{{.Name}}'):
        dsh.run('docker', 'volume', 'rm', workspace)
    task.update(discarded=True, cleaned=True)
    save(directory, task)
    return True


def show_url(box, port, open_browser=False):
    for _ in range(30):
        logs = box.compose('logs', '--no-color', '--tail', '200', 'dsh', capture=True)
        tokens = re.findall(r'http://127\.0\.0\.1:3080/\?token=([^\s]+)', logs)
        if tokens:
            url = f'http://127.0.0.1:{port}/?token={tokens[-1]}'
            print(url)
            if open_browser:
                webbrowser.open(url)
            return
        time.sleep(1)
    print('Browser is still starting. Retry: dsh-task url <task-id>')


def start(args):
    profile = dsh.load_profile(args.profile) if args.profile is not None else {}
    repo = Path(git(args.repo or profile.get('repo') or Path.cwd(), 'rev-parse', '--show-toplevel')).resolve()
    task_id = re.sub('[^a-z0-9]+', '-', args.task.lower()).strip('-')[:40] or 'task'
    task_id += '-' + uuid.uuid4().hex
    target = args.target or git(repo, 'symbolic-ref', '--short', 'HEAD')
    git(repo, 'show-ref', '--verify', 'refs/heads/' + target)
    root = state_root().resolve()
    if root.is_relative_to(repo) or root.is_relative_to(dsh.ROOT):
        raise ValueError('Task state must be outside repositories')
    directory = root / task_id
    directory.mkdir(parents=True, exist_ok=False)
    resolved = dict(name='dsh-task-' + task_id, port=args.port or profile.get('port', 11111),
                    plugin=profile.get('plugin'), key_file=profile.get('key_file'))
    for key in ('plugin', 'key_file'):
        if resolved[key]:
            resolved[key] = str(Path(resolved[key]).resolve(strict=True))
    task = dict(id=task_id, profile=args.profile, repo=str(repo), repository=identity(repo),
                target=target, source_head=git(repo, 'rev-parse', 'HEAD'), resolved=resolved,
                volumes={k: resolved['name'] + '_' + k for k in ('workspace', 'home')}, reviews=[])
    save(directory, task)
    print('Task ID:', task_id, flush=True)
    with locked(task_id), sandbox(task) as box:
        occupied = docker_list('volume', 'ls', '--format', '{{.Name}}')
        if any(name in occupied for name in task['volumes'].values()) or docker_list(
                'ps', '-aq', '--filter', 'label=com.docker.compose.project=' + resolved['name']):
            raise ValueError('Task namespace is occupied')
        images = docker_list('image', 'ls', '--format', '{{.Repository}}:{{.Tag}}')
        if args.rebuild or box.env['DSH_IMAGE'] not in images:
            box.build()
        dsh.transfer(box, args_for(task, 'init', repo=str(repo), revision=args.revision,
                                   snapshot=args.snapshot, include_untracked=args.include_untracked))
        initial = audit(box)
        task.update(base=initial['base'], transfer_host=initial['host'])
        task['volume_created'] = {k: json.loads(dsh.run('docker', 'volume', 'inspect', v,
                                       capture=True))[0]['CreatedAt'] for k, v in task['volumes'].items()}
        save(directory, task)
        box.compose('up', '-d', '--no-build')
        task['container'] = box.compose('ps', '-aq', 'dsh', capture=True)
        task['container'] = json.loads(dsh.run('docker', 'inspect', task['container'], capture=True))[0]['Id']
        save(directory, task)
        show_url(box, resolved['port'], args.open)


def task_for_repository():
    repository = Path(identity(Path.cwd())).resolve()
    matches = []
    for manifest in state_root().glob('*/manifest.json'):
        task = json.loads(manifest.read_text(encoding='utf-8'))
        if (not task.get('cleaned') and not task.get('discard_started')
                and Path(task['repository']).resolve() == repository):
            matches.append(task['id'])
    if len(matches) != 1:
        raise ValueError('Specify a task ID; expected one active task for this repository. Matches: '
                         + (', '.join(sorted(matches)) or '(none)'))
    return matches[0]


def list_tasks():
    rows = []
    for manifest in sorted(state_root().glob('*/manifest.json')):
        try:
            task = json.loads(manifest.read_text(encoding='utf-8'))
            if task['id'] != manifest.parent.name:
                raise ValueError('Task identity mismatch')
            if task.get('discarded'):
                state = 'discarded'
            elif task.get('cleaned'):
                state = 'cleaned'
            elif task.get('discard_started'):
                state = 'discard-incomplete'
            elif task.get('container'):
                state = 'active'
            else:
                state = 'incomplete'
            row = [task['id'], state, task['repo'], task['target'],
                   task.get('profile') or '-', str(task['resolved']['port'])]
            if not all(isinstance(value, str) for value in row):
                raise ValueError('Invalid task summary fields')
            rows.append(row)
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f'Warning: skipping {manifest}: {error}', file=sys.stderr)
    if not rows:
        print('No tasks found.')
        return
    rows.insert(0, ['TASK ID', 'STATE', 'REPOSITORY', 'TARGET', 'PROFILE', 'PORT'])
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    for row in rows:
        print('  '.join(value.ljust(width) for value, width in zip(row, widths)).rstrip())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list', help='List all recorded tasks without contacting Docker')
    child = sub.add_parser('start')
    child.add_argument('--profile', help='Optional saved profile; otherwise use the current repository with no plugin')
    child.add_argument('--task', required=True)
    child.add_argument('--repo')
    child.add_argument('--target')
    child.add_argument('--port', type=int)
    child.add_argument('--revision', default='HEAD')
    child.add_argument('--snapshot', action='store_true')
    child.add_argument('--include-untracked', action='store_true')
    child.add_argument('--rebuild', action='store_true')
    child.add_argument('--open', action='store_true')
    child = sub.add_parser('discard', help='Delete disposable container work without export or review',
        description='Permanently delete this task container and workspace, including uncommitted, '
                    'untracked and ignored files, without export/review/integration. '
                    'Preserve DSH home/history, task records, existing bundles and host worktrees. '
                    'Requires an explicit y at the [y/n] confirmation prompt; otherwise cancels.')
    child.add_argument('task_id', help='Exact task ID to discard after confirmation')
    child = sub.add_parser('import', help='Import a bundle into a local branch without requiring a review')
    child.add_argument('--bundle', required=True)
    child.add_argument('--repo', default='.', help='Destination repository (default: current directory)')
    child.add_argument('--branch', help='New branch name starting with dsh/review- (default: generated)')
    for action in ('show', 'export', 'reviewed', 'prepare', 'finalize', 'cleanup', 'resume', 'url'):
        child = sub.add_parser(action)
        child.add_argument('task_id', nargs='?' if action == 'export' else None)
        if action == 'export':
            child.add_argument('--include-untracked', action='store_true')
            child.add_argument('--bundle', help='Export directly to this file without importing or recording a review')
            child.add_argument('--snapshot', action='store_true', help='Include working-tree edits in a direct bundle export')
            child.add_argument('--force', action='store_true', help='Allow direct bundle export while the sandbox is running')
        if action == 'reviewed':
            child.add_argument('--revision', required=True)
            child.add_argument('--report', required=True)
        if action == 'finalize':
            child.add_argument('--checks', required=True)
        if action == 'cleanup':
            child.add_argument('--require-integrated', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'list':
        list_tasks()
        return
    if args.action == 'import':
        dsh.transfer(None, args)
        return
    if args.action == 'export':
        if not args.bundle and (args.force or args.snapshot):
            parser.error('--force and --snapshot require --bundle')
        if args.bundle and args.include_untracked and not args.snapshot:
            parser.error('--include-untracked requires --snapshot with --bundle')
        if not args.task_id:
            args.task_id = task_for_repository()
    if args.action == 'start':
        if args.include_untracked and not args.snapshot:
            parser.error('--include-untracked requires --snapshot')
        if args.port is not None and not 1 <= args.port <= 65535:
            parser.error('Invalid port')
        start(args)
        return
    with locked(args.task_id) as (directory, task):
        if task.get('discard_started') and args.action not in ('show', 'discard', 'cleanup'):
            raise ValueError('Discard has started; container work is no longer available. Retry discard to finish.')
        if args.action == 'show':
            print(json.dumps(task, indent=2))
        elif args.action == 'discard':
            if discard(directory, task):
                print('Task workspace removed; DSH home/history, task record, existing exports and host worktrees retained.')
        elif args.action == 'reviewed':
            review = latest(task)
            if review['revision'] != args.revision:
                raise ValueError('Review revision mismatch')
            review['report'] = evidence(directory, args.report, 'review')
            save(directory, task)
        elif args.action == 'prepare':
            print(json.dumps(prepare(directory, task), indent=2))
        elif args.action == 'finalize':
            finalize(directory, task, args.checks)
        elif args.action == 'cleanup' and task.get('cleaned'):
            print('Already cleaned; task record and home retained.')
        else:
            if task.get('discard_started'):
                raise ValueError('Discard is incomplete; retry discard to finish.')
            if task.get('cleaned'):
                raise ValueError('Task workspace was cleaned; recovery bundle and home remain')
            with sandbox(task) as box:
                if args.action == 'export':
                    if args.bundle:
                        owned(task)
                        dsh.transfer(box, args_for(task, 'export', bundle=args.bundle,
                                     snapshot=args.snapshot, include_untracked=args.include_untracked,
                                     force=args.force))
                        print('Bundle:', Path(args.bundle).resolve())
                    else:
                        print(json.dumps(export_task(directory, task, box, args.include_untracked), indent=2))
                elif args.action == 'cleanup':
                    cleanup(directory, task, box, args.require_integrated)
                else:
                    owned(task)
                    if args.action == 'resume':
                        box.compose('start')
                    show_url(box, task['resolved']['port'])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
