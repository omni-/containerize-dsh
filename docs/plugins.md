# Plugin contract, version 1

A plugin is a trusted directory selected by `--plugin PATH`. It is not automatically
discovered or an installed package. Only `plugin.json` is required:

```json
{
  "version": 1,
  "runtime": "runtime",
  "compose": "compose.yaml",
  "build": {"context": ".", "dockerfile": "Dockerfile"}
}
```

All fields except `version` are optional. Manifest paths are resolved relative to
the plugin directory and must remain within it. Keep the directory outside the
public repository (the shipped `examples/` is the only allowed in-repo location).
Nothing copies plugin files into public Git state.

| Contribution | Convention |
| --- | --- |
| Derived image / build additions | `build` invokes Docker with `DSH_BASE_IMAGE=dsh-sandbox:local`. Use `ARG DSH_BASE_IMAGE` then `FROM ${DSH_BASE_IMAGE}`; install dependencies as root at build time and end with `USER 10001:10001`. Preserve the core home, workspace, helpers and startup. |
| Runtime files | `runtime` is mounted as a **directory**, read-only at `/opt/dsh-plugin`. Put only agent-readable configuration/scripts here; no host checkout, `.git`, credentials or unrelated files. |
| Cordis/MCP | Optional `runtime/cordis.yml` is passed to `dsh web --patch` on each start. It is a DSH Cordis patch list, not a separate core format. MCP executable paths and cwd refer to the container. |
| Bootstrap | Optional `runtime/bootstrap.sh` runs with `/bin/sh`, as UID 10001, in `/workspace`, before DSH starts. A nonzero result fails startup. Make it idempotent; write only to home, workspace or tmp. |
| Validation | Optional `runtime/validate.sh` runs with `/bin/sh` when the host invokes `validate`, after core checks. Use a nonzero exit for failure. |
| Environment / Compose | Optional `compose` adds environment variables and positive `cpus`, `mem_limit`, `mem_reservation`, `pids_limit`, or `shm_size` limits to the single `dsh` service. Only the core tmpfs sizes and exec/noexec flags can change (syntax below). `${DSH_PLUGIN_DIR}` is the absolute plugin directory; ordinary Compose relative paths otherwise resolve against the public core directory. |

Long-running bootstrap helpers must be backgrounded and manage their own startup
readiness. Docker's init handles children when the container stops. Hooks cannot
install OS packages at runtime; put those in the derived image. For npm MCP packages,
install at build time or into the writable home using a pinned bootstrap command.
Save shell scripts with LF line endings, including on Windows.
Cordis can load executable code: review plugins before selecting them.

The launcher compares merged Compose settings against the core plus launcher-generated
mounts. Only the documented environment, resource limits, tmpfs settings and volume
names/external flags may differ. Other fields, including additional security options,
namespace/runtime/device settings, networks, ports and mount options, are rejected.
Use `--port` for the published web port. No extra services or host mounts are allowed.
These checks catch configuration mistakes; they are not
a security verifier for hostile plugins or Dockerfiles. Plugins are trusted host-side
inputs. Avoid exotic Compose includes/extends and never run an unreviewed plugin.

Tmpfs overrides must contain exactly these two paths, with the options in this order;
change only the positive size (bytes or a `k`, `m`, `g` suffix) and `exec`/`noexec`:

```yaml
services:
  dsh:
    tmpfs: !override
      - /tmp:size=2g,exec,mode=1777,nosuid,nodev
      - /var/tmp:size=1g,exec,mode=1777,nosuid,nodev
```

The `!override` tag requires Compose 2.24.4 or newer and prevents list merging from
leaving duplicate targets. `nosuid`, `nodev` and mode 1777 are fixed. The plugin cannot
mount over `/etc`, `/proc`, `/opt`, home, workspace, or other system paths.

To reuse existing named volumes in a migration, a private override can name them:

```yaml
volumes:
  workspace:
    external: true
    name: existing-workspace-volume
  home:
    external: true
    name: existing-home-volume
```

Never attach those same volumes to a running old sandbox. The transfer helpers
require the base metadata recorded by `init` before they can export or show status.

Use `restart` after editing files in the mounted runtime directory. Directory
mounting also handles editors that replace files atomically. Use `up` to recreate
the container after Compose/environment/key path changes. Use `build` then `up`
after image/dependency changes. No rebuild is needed for ordinary Cordis edits.

The [hello example](../examples/hello) demonstrates a derived image, environment,
bootstrap, validation and a tiny stdio MCP server with a harmless greeting tool.
It requires no external service or API key.
For actual MCP configuration use the pinned DSH package's schema and
[upstream documentation](https://github.com/deepseek-ai/deepseek-harness).
