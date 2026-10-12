# DSH 0.2.1-alpha.2 compatibility review

Compared the upstream `dsh-v0.1.5-rc.3` and `dsh-v0.2.1-alpha.2` source trees,
including CLI dispatch/profile boot, app-boot configuration and environment
loading, web startup, MCP, home paths, and both directory-picker backend/client.
The target is an explicit developer-preview prerelease, not a `latest` tag.
See the [release](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-alpha.2)
and [comparison](https://github.com/deepseek-ai/deepseek-harness/compare/dsh-v0.1.5-rc.3...dsh-v0.2.1-alpha.2).

## Container behavior

- `dsh web` still boots the web surface. CLI aliases now extend to arbitrary
  native profile names; repeatable `--patch` is retained and applies in argument
  order before web app flags. Headless supports `dsh headless`.
- `--no-open`, `--port`, and `--trusted-host` remain supported. Web now supports
  concrete IPv4/IPv6 binds and TLS and rejects wildcard binds. Startup explicitly
  sets `--host 127.0.0.1`; retain the existing socat relay and localhost-only
  Docker publication, including the existing login-token URL handling.
- Cordis patch lists still support inserts and ID-targeted entry-field
  replacements. Upstream replaces whole `config` objects rather than merging
  them. Composition preflight uses DSH's own `loadOverlayPatches`, including its
  `!!js` dialect and relative-module anchoring.
- The example MCP configuration (`serverName`, stdio transport, command, args,
  cwd, `failOnStartupError`) remains compatible. Upstream adds resource/server
  context, reconnect/disposal fixes, and updated MCP dependencies. No alternate
  MCP subsystem or additional host access is introduced.
- DSH's native profiles now have stricter plugin peer-version checks, skipped
  bundle diagnostics, updated optional/official bundle loading, and home-level
  patch layers. Saved containerize-dsh profiles remain host settings; they select
  container plugins and still boot the native `web` profile.
- `HOME=/home/agent`, `DSH_HOME=/home/agent/.dsh`, `/workspace`, and full agent
  permissions within the existing Docker boundary are retained. Upstream adds
  session working-directory/worktree facilities and `DSH_AGENTS_HOME`; its
  default shared agents directory is under the container home, not the host.
- DSH loads inherited environment before workspace `.env` before Harness-home
  `.env`. Bootstrap-only names (including `DSH_*`, `XDG_*`, code-loading and
  trust variables) are rejected from those files; proxy variables are allowed
  in the Harness-home layer. Supply such settings through reviewed Compose
  environment overrides. The host launcher still disables Compose `.env` and
  implicit override loading.
- The existing npm install-script allowlist still builds the new release. No
  Docker privileges, host checkout mounts, socket, or runtime package-install
  permissions were added.

## Directory-browser patch

Keep `scripts/patch-dsh.py`. In the target release,
[the browse backend](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.1-alpha.2/packages/host/directory-picker-browse/src/index.ts)
still uses `resolve(path ?? home)`, and its configuration only exposes
`maxEntries`. The client still opens with an absent path and therefore browses
home. There is no upstream initial-directory setting that can preserve this
repository's `/workspace` default while keeping credentials/history in home.
The existing narrow replacement still matches the published JavaScript and
fails closed if any installed copy changes. Only its version/rationale changed.
The Docker smoke test checks both the default workspace and explicit paths.

## Migration

Rebuild cached core/derived images with `--rebuild` (task startup) or `build`
(low-level launcher). Already-created containers continue using their own image;
ordinary resume does not replace an existing task container. Native plugins in
reused DSH home volumes may need releases declaring compatibility with this DSH
version; inspect DSH's skipped-bundle/refusal diagnostics rather than deleting
home state or bypassing compatibility checks. Review custom patches using
retired upstream fields, such as `both` tool mode or agent-instructions `dshHome`.

Legacy saved `plugin` profiles and recorded task settings continue to work.
New `plugins` profiles reject duplicates and simultaneous singular/plural fields.
Secondary runtime contributions must use their own mount paths; see the
[composition contract](../plugins.md#composition) for build and collision rules.
