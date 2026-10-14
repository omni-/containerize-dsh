# Transfers and snapshots

The host and container have independent Git repositories. Transfers carry Git
objects and selected refs in bundles. The workspace records two starting points:
`refs/dsh/host` holds the chosen host commit, and `refs/dsh/base` holds the exact
transferred base. A snapshot can make those different.

[`dsh.py`](../dsh.py) coordinates the bridge;
[`git_transfer.py`](../scripts/git_transfer.py) builds snapshots and bundles, and
[`workspace.py`](../scripts/workspace.py) manages the container checkout.

## Choosing the starting point

`dsh-task start` transfers committed `HEAD` by default. Choose another commit or ref
with `--revision`, or capture current working files with `--snapshot`:

```sh
dsh-task start --task from-main --revision main
dsh-task start --task current-edits --snapshot
dsh-task start --task with-new-files --snapshot --include-untracked
```

Snapshot mode uses `HEAD`; combining it with another revision fails. It builds a
temporary index from HEAD, reads working-tree contents, and creates a synthetic
child commit if those contents differ. The real index, working files, and host
branch stay as they were. Temporary bundle refs disappear afterward; synthetic
objects remain until Git garbage collection.

By default, snapshots include only paths tracked in HEAD. Newly staged files still
need `--include-untracked`, which adds new, non-ignored files. Ignored files only
travel when HEAD already tracks them. The temporary index captures the final file
contents, not the staged/unstaged split. Stop editing the host while taking a
snapshot; this isn't an atomic filesystem snapshot.

These rules apply recursively to registered submodules. Revision transfers use
the submodule commits pinned by the selected parent commit. Snapshots instead
capture each initialized submodule's current HEAD and working files, including
nested submodules. Each repository gets its own temporary index, so its host
branch, real index, and working files remain unchanged.

The same bundle carries all selected submodule histories and snapshots. Workspace
initialization restores independent submodule checkouts from those objects;
it does not fetch URLs from `.gitmodules`. Make the required commits available
locally before starting, usually with `git submodule update --init --recursive`.
Missing local submodule repositories fail startup before task resources are created.

Review the selected files and history for secrets before transferring them. See
the [isolation model](isolation.md) for what the agent can access.

## Exporting a bundle directly

Normal `dsh-task export <task-id>` follows the
[recorded review workflow](tasks.md#export-and-review). Use `--bundle` when you
only want a file. For an already stopped task:

```text
dsh-task export <task-id> --bundle /private/transfers/work.bundle
dsh-task export <task-id> --snapshot --include-untracked --bundle /private/transfers/dirty-work.bundle
```

Direct export leaves task review records alone. Without `--snapshot`, it requires
a clean tracked/untracked working tree and exports committed work. Untracked
ignored files stay outside the bundle. Existing bundle files are never overwritten.

Direct export requires a stopped sandbox unless you pass `--force`:

```text
dsh-task export <task-id> --force --snapshot --include-untracked --bundle /private/transfers/live-work.bundle
```

`--force` bypasses only the running-sandbox check and preserves the service's
current state. Concurrent edits can make the result inconsistent or cause export
to fail. Snapshot export leaves the live workspace and its index untouched.

You can omit the export task ID when exactly one eligible task belongs to the
current Git repository, including linked worktrees. The lookup excludes cleaned
tasks and tasks whose discard has begun; it reads records rather than checking
whether Docker is running. Pass an ID if the selection is ambiguous.

For direct export, `--include-untracked` requires `--snapshot`. Both `--force` and
`--snapshot` require `--bundle` on `dsh-task export`.

## Importing and reviewing by hand

```sh
dsh-task import --bundle /private/transfers/work.bundle
dsh-task import --repo /path/to/checkout --bundle /private/transfers/work.bundle --branch dsh/review-feature
```

Import needs Git, a destination repository, and the bundle; it needs neither a
task ID nor Docker. The destination defaults to the current directory. Import
creates a new local branch under `dsh/review-` and prints its name, the source host
commit, and the DSH base. It leaves the host branch and working files alone and
refuses to overwrite an existing branch. Nothing merges or pushes automatically.

Submodule histories are preserved under `refs/dsh-submodules/` in the destination
repository, without moving existing submodule checkouts or indexes. Recorded
review exports also list each submodule's `base`, `host`, and `work` commits in
the manifest. To review a submodule, run `git diff <submodule-base>
<submodule-work>` from the main repository; the imported objects are available
there, including for nested submodules.

Before fetching objects, import requires the bundle's recorded host commit to
already exist in the destination. An unrelated repository without that commit
fails the check. The destination can have a newer HEAD, local edits, or a linked
worktree. Import also verifies the expected bundle refs and requires the base to
be an ancestor of the exported work.

Use the printed base to separate agent work from the files supplied at startup:

```text
git diff <base> dsh/review-feature
git log <base>..dsh/review-feature
```

Cherry-pick the commits you want explicitly. A snapshot base already contains your
uncommitted host edits; exclude that base commit when selecting agent-only work.
The task integration workflow instead applies the combined base-to-review diff.

## Repository limits

Bundles include selected commit history and historical file contents. Keep them
private. They omit untracked ignored artifacts and Git LFS object payloads.
Registered submodule repositories are transferred recursively. Unregistered
embedded repositories are refused rather than exported as incomplete gitlinks.
Task audits include submodule indexes, refs, new files, and ignored files;
cleanup requires the same preservation checks in every repository.

Supply ignored artifacts and LFS payloads separately through a private plugin.
Use complete source clones, including the submodules: shallow history can produce
prerequisite-dependent bundles that fail verification. Rebuild cached core and
plugin images with `dsh-task start ... --rebuild` after updating these helpers.

For `init`, `refresh`, workspace status, and the underlying `dsh.py` transfer
commands, see the [launcher reference](launcher.md#workspace-transfers).
