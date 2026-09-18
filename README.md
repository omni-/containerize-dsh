# DSH sandbox

A Docker sandbox for
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (DSH), a coding
agent. DSH gets broad permissions inside the container, while the host checkout,
its `.git`, host credential stores, and Docker socket stay isolated from the agent.
The agent can read credentials explicitly supplied to DSH and secrets included in
transferred Git history.

Work moves through an isolated checkout using Git bundles. External plugins can add
project tooling and MCP configuration and are selected explicitly. The host requires
Python 3.10+, Git, and Docker with Compose v2; Docker Desktop must use Linux
containers on Windows. No Python dependencies are needed.

```text
Dockerfile                 Generic DSH, Node, Git and helper image
compose.yaml               Isolation, localhost web and persistent volumes
dsh.py                     Host launcher and transfer commands
scripts/start-dsh.sh       Authentication, runtime hook and web startup
scripts/git_transfer.py    Temporary-index snapshots and Git bundles
scripts/workspace.py       Container-side checkout operations
examples/hello/            Harmless, optional example plugin
docs/plugins.md            Plugin contract
tests/                     Git and Docker integration tests
```

## Start

Run from this directory (or use the absolute path to `dsh.py`):

```sh
python dsh.py build
python dsh.py init --repo /path/to/host-repository
python dsh.py up
python dsh.py url
```

Open the localhost login URL printed by `url` (it includes DSH's browser token).
Treat that URL as a credential. Enter provider credentials through DSH's UI, or set
`DEEPSEEK_API_KEY` in the launching process. Or pass
`--key-file /outside/public-repo/deepseek-key` before the command. The file contains
only the API key and is mounted read-only; it must be readable by UID 10001.
Credentials entered in DSH persist in its private home volume. No key is required
to start the UI. Authentication with the model provider is separate from web access:
DSH's browser token and localhost publication protect web access; do not forward this
port publicly. The core explicitly trusts the published localhost authority.

Select a plugin explicitly on **every** invocation:

```sh
python dsh.py --name sample --plugin examples/hello build
python dsh.py --name sample --plugin examples/hello init --repo /path/to/repository
python dsh.py --name sample --plugin examples/hello up
python dsh.py --name sample --plugin examples/hello validate
```

An absolute external plugin path works exactly the same way, including paths with
spaces. Choose a distinct `--name` for each project/plugin; it names the persistent
home and workspace volumes. Do not run two sandboxes against the same volumes.
`--port` changes the localhost port. Global options precede the command.
`up` creates/recreates the container without building; run `build` after image changes.

## Saved user profiles

For frequently used plugins, create `containerize-dsh/profiles.json` in your user
config directory. Create its parent directory if needed:

- Windows: `%APPDATA%\containerize-dsh\profiles.json` (normally under `AppData\Roaming`).
- macOS: `~/Library/Application Support/containerize-dsh/profiles.json`.
- Linux: `$XDG_CONFIG_HOME/containerize-dsh/profiles.json`, or
  `~/.config/containerize-dsh/profiles.json` if unset. Relative XDG paths are ignored.

The file is a JSON object keyed by profile name. For example, replacing the paths
with your own absolute paths (Windows paths may use forward slashes):

```json
{
  "my-project": {
    "plugin": "/private/my-project/.dsh-plugin",
    "repo": "/private/my-project",
    "name": "my-project-sandbox",
    "port": 11127,
    "key_file": "/private-secrets/deepseek-key"
  }
}
```

All five fields are optional. `key_file` is a **path**, never an API key; omit it
to use the existing environment/UI authentication. No other fields are accepted.
Saved paths must be absolute or start with `~`; they do not depend on the current
directory. Keep the registry outside this public repository and out of Git.

```sh
python dsh.py --profile my-project up
python dsh.py --profile my-project init
python dsh.py --profile my-project --name another-sandbox --port 11128 up
python dsh.py --profile my-project status --repo /another/host-checkout
```

As with explicit options, run `build` and `init` before the first `up`; `init`
requires an empty workspace. A saved `repo` supplies the default for `init`,
`refresh`, `import`, and `status`; it never mounts the host checkout.
Explicit CLI arguments override saved values, including values equal to the
original defaults. Without `--profile`, the registry is not read and existing
commands work as before. There is no default profile or current-directory plugin
discovery: explicitly select `--profile` or `--plugin` on every invocation.
Selecting a profile trusts its plugin just as passing `--plugin` does; existing
plugin, key-file, and Compose isolation checks still apply. Unknown profiles or
invalid selected entries fail before Docker runs. Edit the JSON file to add,
rename, or remove profiles; the launcher never writes credentials or profiles.

## Work in a separate checkout

The host and `/workspace` are separate Git repositories. The coding agent edits
an isolated checkout; the host checkout is not mounted in the container.
Transfers use Git bundles to carry Git objects and selected refs, never the host
`.git`, remotes, hooks, credential helpers, SSH keys, GitHub credentials, or Docker socket.

Stop the sandbox before any workspace command, including status, unless using
`export --force`. Stopping prevents the agent racing a checkout or snapshot. Do not launch concurrent maintenance
commands or start the service from another terminal while one is running.

```sh
python dsh.py stop
# Use a chosen committed revision:
python dsh.py refresh --repo /path/to/host-repository --revision main
# Or capture current tracked working-tree contents, without changing the real index:
python dsh.py refresh --repo /path/to/host-repository --snapshot
# Opt in to new, non-ignored files as well:
python dsh.py refresh --repo /path/to/host-repository --snapshot --include-untracked
python dsh.py status --repo /path/to/host-repository
python dsh.py up
```

`init` requires an empty volume. `refresh` refuses dirty, untracked **and ignored**
workspace files; it never resets or cleans them. It preserves the old committed
HEAD under `dsh/archive-<id>` before switching to a fresh `dsh/work-<id>` branch.
Commit/export the work and manually archive/remove disposable build outputs, or
use a fresh sandbox name to leave the entire old workspace intact.

Snapshots use a temporary index initialized from HEAD and a synthetic child commit.
They capture working-tree contents, not the staged/unstaged split. By default they
include only paths tracked in HEAD; newly staged files also need
`--include-untracked`. Ignored files are excluded unless already tracked.
Do not edit the host during a snapshot: this is not an atomic filesystem snapshot.
Review the files to be transferred, including tracked secrets, before opting in.
The temporary refs are deleted afterward; synthetic objects remain until Git GC.

## Bring work back for review

```sh
python dsh.py stop
python dsh.py export --bundle /private/transfers/work.bundle
# If the agent has not committed its edits:
python dsh.py export --snapshot --include-untracked --bundle /private/transfers/dirty-work.bundle
# Or export without stopping the running sandbox:
python dsh.py export --force --snapshot --include-untracked --bundle /private/transfers/live-work.bundle
python dsh.py import --repo /path/to/host-repository --bundle /private/transfers/work.bundle --branch dsh/review-feature
```

`export --force` bypasses only the running-sandbox check; it does not stop or restart
the sandbox or overwrite an existing bundle. Concurrent edits can make a live
snapshot inconsistent or cause export to fail.

Import creates a new local review branch and prints the source host commit and DSH
base. Before fetching, it requires the recorded host commit to already exist in the
destination repository; an unrelated repository is rejected. The host may have
advanced since transfer and does not need to have that commit checked out.
It never checks out, merges, pushes, or overwrites an existing branch. It works
with a dirty host and linked Git worktrees. Review with `git diff <base> dsh/review-feature`
and `git log <base>..dsh/review-feature`; cherry-pick the desired commits explicitly.
If the base was a snapshot, it already contains the host's uncommitted edits: exclude
that base commit when selecting agent-only work. Export snapshots do not clean or
commit the live workspace. Existing export files are never overwritten.

`status` prints the original host commit, transferred base, current agent HEAD,
base-vs-HEAD commit counts (base-only then agent-only), and dirty files. With `--repo`
it also shows current host HEAD, whether it changed, and host dirty files, so you can
see divergence even when the repositories have different paths or branches.

Bundles contain the selected commit history, including historical file contents;
keep them private. They do not include ignored artifacts, LFS object payloads or
submodule repositories. Snapshot mode rejects submodules; revision mode carries
gitlinks only. Supply those dependencies separately through your private plugin.
Shallow repositories may yield prerequisite-dependent bundles that fail verification;
use a complete source clone. Transfer a single repository at a time.

## Day-to-day task workflow

The task launcher records each sandbox and its transfers under a unique task ID.
From the repository you want to work on, use the installed command:

```powershell
dsh-task start --task fix-parser --open
# Optional saved settings: --profile my-project
# Optional: --repo <path> --target <branch> --revision <commit-or-ref>
# Dirty baseline instead: --snapshot [--include-untracked]
# Another simultaneous task: --port 11128
# Rebuild core/plugin images explicitly: --rebuild
```

Without `--profile`, the launcher uses the current repository (or `--repo`), no
plugin, port 11111, and `DEEPSEEK_API_KEY` from the environment. You can also enter
credentials in the DSH browser UI. It does not load saved profile settings unless
you explicitly select one. Without the installed command, invoke
`python /path/to/containerize-dsh/dsh_task.py start --task fix-parser --open`.

The profile's sandbox name is replaced with a fresh task namespace.
Task profiles must use task-owned home/workspace volumes, not external/shared
volume overrides. Existing images are reused; rebuild after changing image inputs.
The launcher prints a unique task ID and browser URL. It never mounts the host
checkout. Work with DeepSeek in that browser, then export the task for host-side review.

Task manifests and immutable review bundles live next to the user profile
registry under `tasks/<task-id>/`, outside the source repository. Records include
repository identity, intended target, actual source HEAD, exact transferred base,
resolved settings, Docker identities, review refs/reports and integration results.
Treat this state as private. `python dsh_task.py show <id>` resolves it without conversation
history. Interrupted/failed operations retain their records and resources.

To export only a bundle without creating a review, use:

```powershell
dsh-task export <task-id> --force --snapshot --include-untracked --bundle work.bundle
dsh-task import --bundle work.bundle
```

The task ID may be omitted when exactly one active task belongs to the current
Git repository (including its linked worktrees). `--force` keeps the sandbox
running; concurrent edits may produce an inconsistent snapshot. Without
`--snapshot`, only committed work is exported. Existing bundle files are never
overwritten. Omitting `--bundle` retains the normal review export workflow.

`dsh-task import` defaults to the current directory; use `--repo <path>` for another
repository. It creates a new local branch and prints its name without requiring a
review, switching branches, or merging. Optionally choose its name with
`--branch dsh/review-feature`. Import does not require a task ID or Docker.

Use these host commands to export, record your review, prepare integration, and
clean up after verification:

```text
python dsh_task.py export <id> [--include-untracked]
python dsh_task.py reviewed <id> --revision <exported-oid> --report <review-report>
python dsh_task.py prepare <id>
python dsh_task.py finalize <id> --checks <checks-report>
python dsh_task.py cleanup <id> [--require-integrated]
python dsh_task.py resume <id>
python dsh_task.py url <id>
```

Export pauses DeepSeek and leaves it stopped. New files require explicit inclusion.
Each export creates a new recoverable review ref. Review compares the agent revision
with the recorded transferred base, separating existing dirty baseline changes.
Integration applies only that delta in a separate worktree. Baseline dependencies
or conflicts block preparation; a dirty/moved target blocks finalization. The
prepared branch remains available, and integration is not reported complete.
Reports record human/host-agent verification, not an automatic guarantee of quality.

Cleanup is explicit and refuses changed or unpreserved work, ignored files, and
incomplete requested integration. It removes the exact task container/workspace
volume and clean recorded integration worktrees, preserving home/session history,
recovery bundles/refs/reports, images, caches, plugins and networks. Review alone
never fixes code, changes the target, or deletes the task. Do not operate on the
same task concurrently through lower-level Docker or launcher commands. A leftover
`lock` after process termination should be removed only after confirming no task
operation is still active.

For a genuinely disposable session, explicitly discard its container work instead:

```text
dsh-task discard <task-id>
# Without the installed wrapper: python dsh_task.py discard <task-id>
```

This permanently removes the task container and workspace, including uncommitted,
untracked and ignored files, without requiring export, review or integration.
The command asks for `[y/n]` confirmation before changing resources. Only `y`
(case-insensitive) proceeds; empty input, any other answer, or interrupted input cancels.
Ownership checks still apply. DSH home/session history, task records, existing
exports, host worktrees and shared resources remain. Repeating discard is safe.

## Optional bonus: Codex task helper

The bundled [`dsh-work` Codex skill](skills/dsh-work/SKILL.md) is an **optional bonus**
for reviewing, integrating, and cleaning up completed DSH tasks from the host side.
Codex is not required. The skill uses the same task manifests and Git transfer
commands described above; you can also use the CLI and manual workflow directly.

To install or update it, run from this repository:

```sh
python scripts/install_workflow.py
```

This copies the repository's canonical skill source into your user-level Codex
skills directory (`$CODEX_HOME/skills`, or `~/.codex/skills`) and installs the
`dsh-task` command wrapper. On Windows the installer adds the wrapper to your user
PATH; reopen your terminal afterward. On Linux/macOS ensure `~/.local/bin` is on
PATH. Restart Codex to discover the skill. Keep this checkout and Python interpreter
in place, or rerun the installer after moving them. Make skill edits in the repo
and rerun the installer to refresh the managed copy, including removing stale files.

In host Codex, invoke it explicitly:

```text
$dsh-work review <task-id>
$dsh-work integrate <task-id> and clean up
```

## Isolation and lifecycle

The core runs as UID/GID 10001, with a read-only root, all capabilities dropped,
`no-new-privileges`, writable temporary filesystems, and persistent named volumes
at `/home/agent` and `/workspace`. Internet access remains enabled for DSH and
package downloads. DSH's full-access permission mode operates inside this boundary.
The agent can read its own provider key and mounted plugin runtime files.

`shell`, `logs`, `validate`, `stop`, `restart`, and `down` are host helpers.
`down` removes containers/network but preserves volumes. `dsh.py` has no
volume-delete or workspace-reset command. Back up home separately if you need to
preserve DSH sessions/settings; Git bundles cover only repository contents.

The launcher does not install plugins into this repository or scan project code.
Its generated Compose files and transfers use OS temporary directories. The core
Docker build uses an allowlist `.dockerignore`. Private plugins and secret files must
stay outside this repository; ignored `.local/` is only a guard against accidents,
not the supported plugin location. No `dsh.py` command stages, commits on a host
branch, or publishes anything.

The generic runtime uses Ubuntu 24.04, with Node 24 copied from the official Node
image. This preserves the original runtime's glibc compatibility without including
project SDKs or graphics libraries; those belong in external plugins.
DSH is pinned to the extracted setup's `0.1.5-rc.1`. The Ubuntu and Node tags, apt packages
and npm transitive dependencies can still change between builds; this is not a
fully reproducible dependency lock. The install explicitly permits the native
runtime dependencies' npm install scripts. Review upstream changes when upgrading.

## Tests

```sh
python -m unittest discover -s tests -v
python tests/docker_smoke.py
python tests/task_docker_smoke.py
```

The second command builds local images and uses port 11112, an external copy of the
example plugin, temporary synthetic repositories and fresh uniquely named volumes.
It removes its own containers and volumes afterward. It does not use a real API
key or make a model request. See the [plugin contract](docs/plugins.md).
The third command requires the cached core image, starts a synthetic task with no
provider key, exercises the task lifecycle, and removes only its disposable fixture
resources. It does not build images or make model calls.
Run `python tests/task_docker_smoke.py --discard` to exercise disposable-session
discard, including dirty/ignored files and preservation of DSH home history.
