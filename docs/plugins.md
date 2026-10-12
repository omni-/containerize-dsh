# Plugin contract, version 1

A plugin is a trusted directory selected by a [saved profile](profiles.md)'s
`plugins` list, its legacy `plugin` field, or `--plugin PATH`. It is not automatically
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
| Derived image / build additions | `build` invokes Docker with `DSH_BASE_IMAGE` set to the preceding derived image, or `dsh-sandbox:local` for the first build. Use a global `ARG DSH_BASE_IMAGE` then `FROM ${DSH_BASE_IMAGE}`; install dependencies as root at build time and end with `USER 10001:10001`. Preserve the core home, workspace, helpers and startup. |
| Runtime files | `runtime` is mounted as a **directory**, read-only at `/opt/dsh-plugin` for the first selected plugin, and `/opt/dsh-plugins/2`, `/opt/dsh-plugins/3`, etc. for later selections. Put only agent-readable configuration/scripts here; no host checkout, `.git`, credentials or unrelated files. |
| Cordis/MCP | Each optional `runtime/cordis.yml` is passed as a separate `dsh web --patch`, in selection order, on each start. It is a DSH Cordis patch list, not a separate core format. MCP executable paths and cwd refer to the container. Relative inserted plugin module names are anchored by DSH to their own patch file. |
| Bootstrap | Optional `runtime/bootstrap.sh` runs with `/bin/sh`, as UID 10001, in `/workspace`, before DSH starts. A nonzero result fails startup. Make it idempotent; write only to home, workspace or tmp. |
| Validation | Optional `runtime/validate.sh` runs with `/bin/sh` when the host invokes `validate`, after core checks. Use a nonzero exit for failure. |
| Environment / Compose | Optional `compose` adds environment variables and positive `cpus`, `mem_limit`, `mem_reservation`, `pids_limit`, or `shm_size` limits to the single `dsh` service. Only the core tmpfs sizes and exec/noexec flags can change (syntax below). `${DSH_PLUGIN_DIR}` expands separately to each contributing plugin's absolute directory; ordinary Compose relative paths otherwise resolve against the public core directory. |

Long-running bootstrap helpers must be backgrounded and manage their own startup
readiness. Docker's init handles children when the container stops. Hooks cannot
install OS packages at runtime; put those in the derived image. For npm MCP packages,
install at build time or into the writable home using a pinned bootstrap command.
Save shell scripts with LF line endings, including on Windows.
Cordis can load executable code: review plugins before selecting them.

## Composition

The manifest remains contract version 1. A profile's ordered list is the complete
selection, with no dependency discovery. Empty lists select no plugins. Duplicate
resolved directory paths and profiles specifying both `plugin` and `plugins` fail.

- **Builds:** build the core once, then each selected build in order, passing its
  predecessor as `DSH_BASE_IMAGE`. Runtime-only plugins do not add image layers.
  Stack image tags hash ordered path prefixes; the established single-plugin tag
  is preserved. In a multi-plugin selection, each Dockerfile must declare the
  global base argument and its final stage must descend from that supplied image,
  directly or through a named/numeric stage. A hardcoded base that discards earlier
  layers is rejected before a build, with both plugin paths. Dockerfile commands
  remain trusted sequential image changes; authors must keep installed packages
  and shared image files compatible. This does not inspect arbitrary `RUN` code.
- **Runtime files:** each directory gets its own mount, so two plugins may have
  different `data.json`, `bootstrap.sh`, or `cordis.yml` files without overwriting
  each other. Position is based on the full selected list, including plugins
  without runtime files. There is no merged runtime filesystem. Single-plugin
  paths remain unchanged. Hooks receive `DSH_PLUGIN_RUNTIME` for their own mount;
  use it to locate sibling files. Later plugins' absolute Cordis/MCP paths must
  use their own `/opt/dsh-plugins/N` mount. Plugins hardcoding `/opt/dsh-plugin`
  should stay first or update those references before being moved later.
- **Cordis:** parse with the pinned DSH parser, including `!!js`, then pass the
  patches in profile order. Inserted IDs (including nested groups) and literal
  MCP `serverName` values must be unique across plugins. Group inserts append
  sequentially. Entry fields, including the entire `config` object, are atomic:
  different plugins may set disjoint fields or identical values, but differing
  values fail. Replacing a group's config while another plugin appends children
  also fails. There is no extra deep merge of Cordis configuration. A plugin's
  own internal patch sequence retains DSH behavior. Expressions and executable
  plugins still undergo DSH's runtime validation; the static conflict check does
  not evaluate dynamic expressions or inspect code-created contributions.
- **Hooks:** all bootstrap hooks run in order before DSH; all validation hooks
  run in order after core validation. Each runs with `/bin/sh`, UID 10001 and
  `/workspace` as cwd. The first failure stops the sequence and names its plugin.
  Check all Cordis contributions before running any bootstrap hook. Startup and
  restart preflight inherits validated isolation/resource settings and runtime
  mounts, with no network, published ports, task volumes, or key-file mount. A
  rejected check leaves no task resources. Resume uses
  the recorded container's entrypoint check, preserving older task images.
- **Compose:** render and validate each override individually before combining
  it. Environment keys and each resource field compose independently. Explicit
  values equal to core defaults still count as contributions. Equal normalized
  values may agree; different values for the same field fail. The complete tmpfs
  pair and each named-volume definition are atomic contributions and must agree
  if repeated. Validate the final combination again, including distinct home
  and workspace volume names. Launcher-owned runtime metadata and key-file
  settings retain precedence. Unsupported isolation changes fail even if a
  later plugin would undo them.

Compose and build errors occur before building or starting the sandbox. Cordis
preflight requires the built image. Task startup checks it before workspace
transfer; low-level `up`/`restart` check it before starting hooks. The entrypoint
also checks on every start, including resume and direct Compose use.
Conflict errors identify both plugin paths and the contribution. All selected
paths are recorded at task creation; profile edits do not retarget existing tasks.
Referenced files remain live inputs, so editing plugin files still affects later
starts. No-plugin and single-plugin command syntax remains unchanged.

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
