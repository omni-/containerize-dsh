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
the existing stopped web container before printing it. A headless task's first
resume replaces its stopped container with the web service, using the recorded
image and the same home/workspace volumes. Later resumes start that web container.
Resume doesn't select a new profile or rerun the headless prompt.

## Run a headless task

```sh
dsh-task run --task benchmark --repo /path/to/prepared-repo --prompt-file /private/prompt.txt --timeout 600
```

`run` shares startup with `start`: it resolves the profile, builds missing images,
transfers the repository, and records the task and volumes. It accepts `--profile`,
`--repo`, `--target`, `--revision`, `--snapshot`, `--include-untracked`, and
`--rebuild` with the same meanings. It publishes no port and opens no browser.
Rebuild cached images after updating the runtime scripts in this repository.

| Run option | Effect |
| --- | --- |
| `--prompt-file PATH` | Required UTF-8 prompt file. Use `-` to read stdin; empty prompts fail before startup. |
| `--events PATH` | Write stdout events to a new host NDJSON file. Existing files are refused; the parent directory must exist. Defaults to `events.ndjson` in the task's state directory. |
| `--timeout SECONDS` | Limit the headless process after workspace setup. Must be positive and finite; omitted means no limit. |
| `--model MODEL` | Override the composed default model. `provider/model` also selects the registered provider route; a bare model ID keeps the provider. |
| `--effort EFFORT` | Override `reasoningEffort` in the shared model configuration. Supported values depend on the selected provider and model. |

Model and effort overrides use a final Cordis patch over DSH's composed
`agent-default-model` configuration. Unspecified fields keep their composed
values, so `--effort` alone preserves the configured provider and model. These
options don't add provider routes. Plugins still supply their ordered
`cordis.yml` patches and bootstrap hooks. The hooks run as the agent before DSH;
their output stays outside the JSON stream. Headless runs use the container's
`DSH_PERMISSION_MODE=danger-full-access` preset.

The prompt travels through Docker stdin into the recorded container, without a
host mount or a command-line argument. Provider credentials use the launch
environment or the profile's `key_file`, with the same precedence as web tasks.
Credentials saved in the GUI are ignored by headless mode.

The first output line is `Task ID: <id>`, followed by `Events: <path>`. Events
stream directly to that host file while the command waits. The optional
`headless` object in `show` records UTC `started_at` and `ended_at`, the container's
`exit_code`, `final_reason` from `final.reason.kind`, an `error` event's message
or launcher failure, the `events` path, `session_id`, `timed_out`, and requested
`model`/`effort`. Records from older tasks need no new fields.

Exit status is 0 only when the process and Docker attachment exit 0, the final
event reports `completed`, and no stream error or timeout occurred. Other run
outcomes exit 1; invalid arguments exit 2. A timeout stops the recorded container
with a ten-second grace period before Docker forces termination. It keeps the
volumes and partial event stream. The timeout excludes transfer and image builds.

Both successful and failed runs can use normal export and review, direct bundle
export, cleanup, or discard. For a benchmark bundle containing working files:

```sh
dsh-task export <task-id> --snapshot --include-untracked --bundle /private/work.bundle
```

Use `resume <task-id>` to inspect the session in the web UI; `url` becomes useful
after that first web startup. A headless outcome remains recorded after resume.

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

Export includes registered submodules recursively, with the same opt-in for new
files inside them. The review record lists their starting and exported commits
so their diffs can be reviewed from the host repository.

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

For changed submodules, preparation creates independent checkouts in that worktree and
applies the agent's changes separately in each repository. It commits those
changes from the target's submodule revisions, then updates the parent gitlinks.
Edits supplied in the initial snapshot stay out of the integrated submodule trees.

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
Submodule histories are loaded locally, and finalization updates their checkouts
with network fetching disabled. Dirty submodules and ignored-file collisions
block finalization before the target branch advances.
Unchanged, uninitialized dependencies stay uninitialized on the host; their
objects remain preserved. Changing a submodule requires a prepared checkout,
so dependencies with filenames unsupported by the host need integration on a
compatible system.

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
the target branch, source HEAD, transferred base, resolved settings (including
the fully resolved ordered `plugins` list), Docker
identities, review bundles/refs/reports, and integration results. Keep it private.
Failed or interrupted operations leave their records and resources for recovery.
Old task records with singular `plugin` are normalized when read without consulting
the current saved profile. Later profile edits do not alter any recorded selection.

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

## Optional Codex and Claude Code helpers

The [bundled `dsh-work` skill](../skills/dsh-work/SKILL.md) lets Codex or Claude Code
on the host review, integrate, and clean up completed tasks using the same CLI and
records. Both versions require explicit invocation. In Codex:

```text
$dsh-work review <task-id>
$dsh-work integrate <task-id> and clean up
```

In Claude Code:

```text
/dsh-work review <task-id>
/dsh-work integrate <task-id> and clean up
```

Run `python scripts/install_workflow.py` from this checkout to install or update
both skill versions and the command wrapper. The Codex version goes into
`$CODEX_HOME/skills/dsh-work`, or `~/.codex/skills/dsh-work`. The Claude Code version
goes into `$CLAUDE_CONFIG_DIR/skills/dsh-work`, or `~/.claude/skills/dsh-work`, with
`disable-model-invocation: true` in its frontmatter. Restart your app to discover
the skill. The wrapper goes into
`%LOCALAPPDATA%\containerize-dsh\bin` on Windows or `~/.local/bin` on Linux/macOS.
Windows installation updates user PATH.

Edit the shared canonical skill in this repository and rerun the installer.
Updates replace both managed copies, including removing stale files and local
edits there. The installer checks both skill destinations before writing and
refuses to overwrite an unrelated wrapper or either unmanaged skill.
