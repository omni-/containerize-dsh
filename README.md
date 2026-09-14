# DSH sandbox

A small Docker home for [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness),
with explicitly selected external project plugins and an isolated Git checkout.
Requires Python 3.10+, Git, and Docker with Compose v2 on the host; Docker Desktop
must use Linux containers on Windows. No Python dependencies are needed.

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
`DEEPSEEK_API_KEY` in the launching process. Alternatively supply
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

For frequently used plugins, create a `containerize-dsh/profiles.json` file in
your user config directory (create its parent directory if needed):

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

The host and `/workspace` are different Git repositories, like two machines.
The agent can edit only its own checkout. It cannot operate on a host path.
Transfers carry Git objects and selected refs, never the host `.git`, remotes,
hooks, credential helpers, SSH keys, GitHub credentials, or Docker socket.

Stop the sandbox before any workspace command, including status. This prevents
the agent racing a checkout or snapshot. Do not launch concurrent maintenance
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
python dsh.py import --repo /path/to/host-repository --bundle /private/transfers/work.bundle --branch dsh/review-feature
```

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
it also shows current host HEAD, whether it changed, and host dirty files. This makes
divergence visible without assuming the repositories have matching paths or branches.

Bundles contain the selected commit history, including historical file contents;
keep them private. They do not include ignored artifacts, LFS object payloads or
submodule repositories. Snapshot mode rejects submodules; revision mode carries
gitlinks only. Supply those dependencies separately through your private plugin.
Shallow repositories may yield prerequisite-dependent bundles that fail verification;
use a complete source clone. Transfer a single repository at a time.

## Isolation and lifecycle

The core runs as UID/GID 10001, with a read-only root, all capabilities dropped,
`no-new-privileges`, writable temporary filesystems, and persistent named volumes
at `/home/agent` and `/workspace`. Internet access remains enabled for DSH and
package downloads. DSH's full-access permission mode operates inside this boundary.
The agent can read its own provider key and mounted plugin runtime files.

`shell`, `logs`, `validate`, `stop`, `restart`, and `down` are host helpers.
`down` removes containers/network but preserves volumes. There is deliberately no
volume-delete or workspace-reset command. Back up home separately if you need to
preserve DSH sessions/settings; Git bundles cover only repository contents.

The launcher neither installs plugins into this repository nor scans project code.
Its generated Compose files and transfers use OS temporary directories. The core
Docker build uses an allowlist `.dockerignore`. Private plugins and secret files must
stay outside this repository; ignored `.local/` is only a guard against accidents,
not the supported plugin location. No launcher command stages, commits on a host
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
```

The second command builds local images and uses port 11112, an external copy of the
example plugin, temporary synthetic repositories and fresh uniquely named volumes.
It removes its own containers and volumes afterward. It does not use a real API
key or make a model request. See the [plugin contract](docs/plugins.md).
