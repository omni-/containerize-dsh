# DSH 0.2.1-rc.2 compatibility review

Compared the upstream `dsh-v0.2.1-alpha.2` and `dsh-v0.2.1-rc.2` source trees,
including CLI dispatch/profile boot, app-boot configuration and environment
loading, web startup, MCP, home paths, and both directory-picker backend/client.
The target is an explicit release-candidate prerelease, not a floating tag.
See the [release](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.2)
and [comparison](https://github.com/deepseek-ai/deepseek-harness/compare/dsh-v0.2.1-alpha.2...dsh-v0.2.1-rc.2).
The [previous review](dsh-0.2.1-alpha.2.md) covers the larger migration from
`0.1.5-rc.3` to the earlier `0.2.1` preview.

## Container behavior

- `dsh web` still boots the web surface. Repeatable `--patch` remains supported
  and applies in argument order before web app flags. `dsh headless --help`
  remains available for the headless surface.
- `--no-open`, `--host`, `--port`, and `--trusted-host` remain supported.
  Startup continues to bind DSH to `127.0.0.1`, relay it inside the container,
  and publish the relay only on host localhost. Browser token URL handling is
  unchanged.
- Cordis patch lists still support inserts and ID-targeted entry-field
  replacements. Composition preflight continues to use DSH's own
  `loadOverlayPatches`, including its `!!js` dialect and relative-module
  anchoring.
- The example MCP configuration (`serverName`, stdio transport, command, args,
  cwd, `failOnStartupError`) remains compatible. No alternate MCP subsystem or
  additional host access is introduced.
- DSH now prioritizes saved credentials for Web and environment or `.env`
  credentials for headless. The persistent home volume and explicit Compose
  environment continue to provide both supported paths; the UI can still start
  without a provider key.
- Third-party native-plugin compatibility now prefers `engines.dsh` when it is
  declared. Plugins stored in a reused DSH home volume may therefore need a
  release whose range includes `0.2.1-rc.2`.
- Upstream adds local Web account login, plugin refresh fixes, model/runtime
  improvements, DeepSeek-V4.1-Flash-1017 support, and optional DeepSeek Search
  and Fetch APIs. These additions do not require new container privileges.
- `HOME=/home/agent`, `DSH_HOME=/home/agent/.dsh`, `/workspace`, and full agent
  permissions within the existing Docker boundary are retained. The existing
  npm install-script allowlist still builds the release.

## Directory-browser patch

Keep `scripts/patch-dsh.py`. The published browse backend is unchanged between
the old and new tags (Git blob `01fe5b9cd23e9c3318ed94db4f0f4a1488e46ba5`).
[The target source](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-rc.2/packages/host/directory-picker-browse/src/index.ts)
still uses `resolve(path ?? home)`, and its configuration only exposes
`maxEntries`. The client still opens with an absent path and therefore browses
home. There is no upstream initial-directory setting that can preserve this
repository's `/workspace` default while keeping credentials/history in home.
The existing narrow replacement still matches the published JavaScript and
fails closed if an installed copy changes. The Docker smoke test checks both
the default workspace and explicit paths.

## Migration

Rebuild cached core/derived images with `--rebuild` (task startup) or `build`
(low-level launcher). Already-created containers continue using their original
image; ordinary resume does not replace an existing task container. Inspect
DSH's skipped-bundle or compatibility diagnostics before changing native plugin
state in a reused home volume.

Legacy saved `plugin` profiles and recorded task settings continue to work.
New `plugins` profiles reject duplicates and simultaneous singular/plural fields.
Secondary runtime contributions must use their own mount paths; see the
[composition contract](../plugins.md#composition) for build and collision rules.
