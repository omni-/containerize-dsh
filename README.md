# DSH sandbox

Run [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH), a
coding agent, in Docker and bring its changes back to Git for review. `dsh-task`
gives each session a separate checkout, records its work, and helps you review,
integrate, and clean up when you're done. Git bundles carry work between the host
and container. Optional plugins supply project tools and MCP configuration.

## Requirements

You need Python 3.10+, Git, and Docker with Compose v2. On Windows, set Docker
Desktop to Linux containers. The host scripts use only Python's standard library.
Start from a Git repository with at least one commit; configure your Git author
name and email if you plan to integrate changes.

## Quick start

From this checkout, install the command:

```sh
python scripts/install_workflow.py
```

On Windows, reopen your terminal to pick up the updated user PATH. On Linux/macOS,
put `~/.local/bin` on PATH. The installer also adds the optional `dsh-work` Codex
skill; Codex isn't required to use the sandbox. Keep this checkout and Python
interpreter in place, or rerun the installer after moving them.

Then, from the repository you want DeepSeek to work on:

```sh
dsh-task start --task fix-parser --open
```

This transfers `HEAD` and opens DSH in your browser. The current branch becomes
the integration target; choose another with `--target`. Enter provider credentials
in the UI, or set `DEEPSEEK_API_KEY` before starting the task. The UI can start
without a provider key. For a key file or project plugin, use a
[saved profile](docs/profiles.md):

```sh
dsh-task start --task fix-parser --profile my-project --open
```

Without a profile, the defaults are the current repository, no plugin, and port
11111. Each start creates a unique task ID and its own home/workspace volumes.
Use `--port 11128` for another simultaneous task. Startup builds missing images;
pass `--rebuild` after changing image inputs to replace a cached image.

To send your current edits instead of committed `HEAD`, add `--snapshot`.
Add `--include-untracked` only after checking which new files you want to share:

```sh
dsh-task start --task fix-parser --snapshot --include-untracked --open
```

Snapshots capture file contents without changing your real index or branch. They
don't preserve the staged/unstaged split, and concurrent host edits can produce an
inconsistent snapshot. See [transfer rules](docs/transfers.md) for file selection
and repository limits.

If you prefer to skip installation, replace `dsh-task` in these commands with
`python /path/to/containerize-dsh/dsh_task.py`.

## Work on a task

Work with DeepSeek in the browser, then export when you're ready to review:

```text
dsh-task export <task-id>
```

Export stops DeepSeek and leaves it stopped. It captures committed and uncommitted
tracked work, saves a recovery bundle, and creates a new review branch in the host
repository. It refuses new, uncommitted files unless you add `--include-untracked`.

The output includes the exported revision and review branch; `show` includes the
transferred `base`. Compare against that base so any edits you supplied at startup
stay out of the agent's diff:

```text
dsh-task show <task-id>
git diff <base> <review-branch>
git log <base>..<review-branch>
```

Write your findings in a nonempty report, then record the exact exported revision
and prepare the change. Keep review and checks reports outside the host and
integration worktrees so they stay clean:

```text
dsh-task reviewed <task-id> --revision <exported-oid> --report <review-report>
dsh-task prepare <task-id>
```

`prepare` applies the diff from the transferred base in a separate host worktree
and commits it there. Inspect that worktree, run the project's checks, and save
the results in a checks report. To finish:

```text
dsh-task finalize <task-id> --checks <checks-report>
dsh-task cleanup <task-id> --require-integrated
```

`finalize` fast-forwards the intended target branch. It requires the original host
checkout to be clean, on that branch, and still at the commit used by `prepare`.
Conflicts during preparation or a changed target leave the prepared branch
available for recovery. Review and checks reports record your verification; the
launcher doesn't judge code quality or run those checks for you.

Cleanup refuses work that changed after export, ignored files, and work it cannot
preserve in the review. It removes the task container, workspace volume, and clean
recorded integration worktrees, while keeping DSH home/session history and recovery
records. You can also clean up a reviewed task without integrating it; see the
[task lifecycle](docs/tasks.md) for the conditions.

To continue working, use `dsh-task resume <task-id>`. `dsh-task url <task-id>` prints
the login URL again, and `dsh-task list` finds saved task IDs from any directory.
Its states come from local records, so `active` doesn't prove a container is
running. For a disposable session, `dsh-task discard <task-id>` asks for confirmation
and **permanently deletes all container workspace files**, including uncommitted,
untracked, and ignored work, without a review.

## Isolation and security

The agent gets full DSH permissions inside the container. The core runs as UID/GID
10001 with a read-only root, dropped capabilities, `no-new-privileges`, writable
temporary filesystems, and persistent home/workspace volumes. Internet access stays
enabled for DSH and package downloads.

The host checkout, its `.git`, credential stores, SSH keys, and Docker socket stay
outside the container. The agent can read credentials you supply to DSH, plugin
runtime files, and any secrets in transferred Git history. Keep bundles and task
records private, and review what you transfer. Plugins are trusted inputs; the
Compose checks catch configuration mistakes, not hostile plugin code.

The web UI publishes only on localhost. Its login URL contains a browser token:
treat the URL as a credential and don't forward the port publicly. Provider
authentication is separate from browser access. Credentials entered in DSH persist
in its home volume.

## Reference

- [Profiles](docs/profiles.md): saved paths, ports, plugins, and key files.
- [Transfers](docs/transfers.md): snapshots, direct bundle export, imports, and manual review.
- [Task lifecycle](docs/tasks.md): command options, records, recovery, cleanup, and the Codex helper.
- [Isolation details](docs/isolation.md): mounts, authentication, build inputs, and trust boundaries.
- [Low-level launcher](docs/launcher.md): `dsh.py` commands for managing a sandbox directly.
- [Plugin contract](docs/plugins.md): project tooling, runtime hooks, and the hello example.
- [Testing](docs/testing.md): local tests and Docker smoke tests.
