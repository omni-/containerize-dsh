# Low-level launcher

Use `dsh-task` for the [normal task workflow](../README.md#work-on-a-task).
`dsh.py` manages a sandbox directly, without task records or an integration workflow.
Run these examples from this repository, or use an absolute path to `dsh.py`.

## Setup

```sh
python dsh.py build
python dsh.py init --repo /path/to/host-repository
python dsh.py up
python dsh.py url
```

The defaults are sandbox name `dsh-sandbox` and localhost port 11111. Open the
printed login URL and use one of the [authentication options](isolation.md#authentication).
`up` creates or recreates the container without building; run `build` after changing
image inputs. `init` needs an empty workspace volume.

Global options precede the command: `--profile`, `--name`, `--plugin`, `--key-file`,
and `--port`. Select a distinct name for each project/plugin; the name identifies
its persistent home and workspace volumes. Never run two sandboxes against the
same volumes.

Select a plugin explicitly on every invocation, either directly or through a
[profile](profiles.md):

```sh
python dsh.py --name sample --plugin examples/hello build
python dsh.py --name sample --plugin examples/hello init --repo /path/to/repository
python dsh.py --name sample --plugin examples/hello up
python dsh.py --name sample --plugin examples/hello validate
```

Absolute external plugin paths work too; quote paths containing spaces. The
[hello example](../examples/hello) needs no external service. Follow the
[plugin contract](plugins.md) when adding project dependencies.

## Workspace transfers

Stop the sandbox before `init`, `refresh`, `export`, or `status`. The exception is
`export --force`, with the [live export caveat](transfers.md#exporting-a-bundle-directly).
Maintenance commands use short-lived containers against the same volumes, so don't
run them concurrently or start the service from another terminal while they run.

```sh
python dsh.py stop
python dsh.py refresh --repo /path/to/host-repository --revision main
python dsh.py status --repo /path/to/host-repository
python dsh.py up
```

Instead of `--revision main`, use `--snapshot` to capture current tracked working
files, adding `--include-untracked` for new files. The same options work with `init`.
See [snapshot rules](transfers.md#choosing-the-starting-point) before choosing files.

`refresh` refuses dirty, untracked, and ignored workspace files. Commit/export
valuable work and manually archive or remove disposable build outputs first. It
preserves the old committed HEAD under `dsh/archive-<id>` and switches to a fresh
`dsh/work-<id>` branch. It never resets or cleans the old checkout for you. A new
sandbox name leaves the entire old workspace intact.

`status` reports the original host commit, transferred base, current agent HEAD,
base-only and agent-only commit counts (in that order), and dirty files. With
`--repo`, it also reports current host HEAD, whether it changed since transfer,
and host dirty files.

```sh
python dsh.py stop
python dsh.py export --bundle /private/transfers/work.bundle
# For uncommitted work, use this export instead:
python dsh.py export --snapshot --include-untracked --bundle /private/transfers/dirty-work.bundle
python dsh.py import --repo /path/to/host-repository --bundle /private/transfers/work.bundle --branch dsh/review-feature
```

These commands use the same [export and import rules](transfers.md) as direct task
bundles. Import runs entirely on the host. No `dsh.py` command stages changes in
the real host index, commits on a host branch, or publishes anything.

## Service commands

| Command | Use |
| --- | --- |
| `shell` | Open Bash in the running container. |
| `logs` | Read service logs; these can contain the browser token. |
| `validate` | Check the runtime UID, writable home/workspace, and DSH version, then run any plugin validation hook. |
| `config` | Validate merged Compose isolation settings without printing secrets. |
| `stop` | Stop the service, keeping its container and volumes. |
| `restart` | Restart after editing mounted runtime files. |
| `down` | Remove the service containers/network, preserving volumes. |

Use `up` after Compose, environment, or key-path changes, and `build` then `up`
after image/dependency changes. The low-level launcher has no volume-delete or
workspace-reset command. For task-owned resources, follow
[task cleanup or discard](tasks.md#cleanup-or-discard) rather than changing their
lifecycle through this interface.
