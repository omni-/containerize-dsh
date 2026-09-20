# Task lifecycle

`dsh-task` tracks a sandbox from startup through review and cleanup. The
[README](../README.md#work-on-a-task) walks through the usual sequence; this page
covers options and recovery. Without the installed wrapper, invoke
`python /path/to/containerize-dsh/dsh_task.py` with the same arguments.

## Start and find tasks

```sh
dsh-task start --task fix-parser --open
dsh-task list
```

`--task` supplies a readable label. The launcher normalizes it and appends a UUID
to create the full task ID. Each task uses a unique Docker namespace. Startup
reuses an existing image or builds a missing one, initializes the workspace, starts
DSH, and prints the task ID and browser URL.

| Start option | Effect |
| --- | --- |
| `--profile NAME` | Load explicit [saved settings](profiles.md). |
| `--repo PATH` | Choose the host checkout; otherwise use the profile's repo or current repository. |
| `--target BRANCH` | Choose an existing local branch for integration; defaults to the current branch. Supply it when starting from detached HEAD. |
| `--revision REF` | Transfer this commit or ref; defaults to `HEAD`, independently of `--target`. |
| `--snapshot` | Transfer working-tree contents using the [snapshot rules](transfers.md#choosing-the-starting-point). |
| `--include-untracked` | Include new files in a snapshot. Requires `--snapshot`. |
| `--port NUMBER` | Override the profile port or default 11111. |
| `--rebuild` | Build the core and any plugin image even if cached. |
| `--open` | Open the login URL in your browser. |

`list` works from any directory. It reads local manifests, sorts by full task ID,
and shows the repository, target branch, profile, port, and recorded state.
`active` means startup recorded a container; `incomplete` means it didn't.
`discard-incomplete`, `discarded`, and `cleaned` describe later lifecycle steps.
The list never contacts Git or Docker and skips unreadable or malformed records
with a warning.

Use these commands to inspect a task or return to the browser:

```text
dsh-task show <task-id>
dsh-task url <task-id>
dsh-task resume <task-id>
```

`show` prints the saved manifest. `url` prints the login URL, and `resume` starts
the existing stopped container before printing it. Resume uses the recorded
container; it doesn't recreate it with changed profile settings.

## Export and review

```text
dsh-task export <task-id> --include-untracked
dsh-task reviewed <task-id> --revision <exported-oid> --report <review-report>
```

Normal export stops DSH, snapshots the workspace, imports a fresh review branch
into the original host repository, and records the bundle's hash and workspace
state. New, uncommitted files require `--include-untracked`, even if staged.
Each export gets its own bundle and review ref, so another export leaves older
reviews recoverable.

Review the diff from the manifest's `base` to the exported `revision`. Record your
findings in a nonempty file and pass that exact revision to `reviewed`. Keep this
report and the later checks report outside the host and integration worktrees.
If the review finds a problem, resume the task to address it, then export and
review the new revision before integration.

The latest export is the one used by `reviewed`, `prepare`, `finalize`, and
`cleanup`. These commands verify its saved bundle hash and refs. Editing a bundle
or moving its review branch blocks that workflow. For export without recording a
review, use [direct bundle export](transfers.md#exporting-a-bundle-directly).

## Prepare and finalize

```text
dsh-task prepare <task-id>
dsh-task finalize <task-id> --checks <checks-report>
```

`prepare` requires a review report, creates a `dsh/integrate-...` branch and a
worktree under the task directory, then applies the binary diff from the transferred
base to the reviewed revision. It commits a nonempty diff as one integration
commit with Git hooks disabled. An empty diff leaves the target commit as the
prepared revision. This keeps a synthetic baseline's ancestry out of the target.

Preparation starts from the target branch's current commit, so unrelated host
advances since task startup can coexist with the agent's change. A patch conflict,
including one caused by missing baseline edits, leaves a worktree and a `blocked`
integration record for inspection. Resolve the underlying problem and prepare
again; an edited or blocked worktree cannot go straight through `finalize`.
A patch that applies cleanly can still depend on baseline edits, so check the
prepared result on the target before finalizing.

Inspect and test the prepared worktree, then supply a nonempty checks report.
Finalization requires the prepared worktree to be clean and still at its recorded
revision. The original host checkout must own the intended target branch, be
clean, and remain at the commit used for preparation. A failure leaves integration
incomplete and keeps the prepared branch. The merge uses fast-forward only, with
hooks disabled, no autostash, and protection against overwriting ignored files.

Reports document verification by a person or host agent. The launcher requires
nonempty reports; it doesn't interpret the findings or run project tests.

## Cleanup or discard

```text
dsh-task cleanup <task-id>
dsh-task cleanup <task-id> --require-integrated
```

Cleanup requires a report for the latest export and verifies that the workspace
still matches it. It refuses ignored workspace files and refs whose commits aren't
ancestors of the exported revision. Archive ignored outputs outside the workspace
before exporting again. Recorded integration worktrees must also be unchanged and
free of dirty, untracked, or ignored files.

If you requested integration with `prepare`, or pass `--require-integrated`, the
latest review must have completed integration and the target must still contain
the integrated commit. Without either condition, a reviewed task can be cleaned up
without changing the target. A blocked earlier worktree can still need attention
before cleanup will proceed.

After those checks, cleanup removes the recorded container, workspace volume, and
eligible integration worktrees. It keeps the home volume with DSH sessions,
task records, bundles, reports, recovery branches, images, caches, plugins, and
networks. Repeating successful cleanup is safe.

To abandon container work instead:

```text
dsh-task discard <task-id>
```

Discard permanently removes the recorded container and workspace, including dirty,
untracked, and ignored files, without requiring export, review, or integration.
Only `y` (case-insensitive, allowing surrounding whitespace) at the `[y/n]` prompt
proceeds; empty, other, or interrupted input cancels. Ownership checks still apply.
Discard leaves host worktrees and the resources cleanup normally preserves alone.
Repeating a completed discard is safe; retry an interrupted discard to finish it.

## Records and recovery

Task state lives beside the [profile registry](profiles.md), under
`tasks/<task-id>/`, outside source repositories. It includes repository identity,
the target branch, source HEAD, transferred base, resolved settings, Docker
identities, review bundles/refs/reports, and integration results. Keep it private.
Failed or interrupted operations leave their records and resources for recovery.

Review and integration verify the source repository's Git common directory.
Docker ownership checks compare the recorded container, volume labels and creation
times, and all volume consumers before stopping or removing resources. Another
container using the volumes, or a recreated volume with the same name, blocks the
operation.

Don't operate on one task concurrently through Docker or the low-level launcher.
Task commands use a per-task `lock` file, but those other interfaces bypass it.
After a terminated process, remove a leftover lock only once you've confirmed no
task operation is still running. Inspect `show` and the saved records before
deciding how to recover an incomplete startup; starting again creates another task.

## Optional Codex helper

The [bundled `dsh-work` skill](../skills/dsh-work/SKILL.md) lets host Codex review,
integrate, and clean up completed tasks using the same CLI and records. Invoke it
explicitly:

```text
$dsh-work review <task-id>
$dsh-work integrate <task-id> and clean up
```

Run `python scripts/install_workflow.py` from this checkout to install or update
both the skill and command wrapper. The skill goes into `$CODEX_HOME/skills`, or
`~/.codex/skills`; restart Codex to discover it. The wrapper goes into
`%LOCALAPPDATA%\containerize-dsh\bin` on Windows or `~/.local/bin` on Linux/macOS.
Windows installation updates user PATH.

Edit the canonical skill in this repository and rerun the installer. Updates
replace the managed copy, including removing stale files and local edits there.
The installer refuses to overwrite an unrelated wrapper or unmanaged skill.
