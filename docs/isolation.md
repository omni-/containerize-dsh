# Isolation details

The [README](../README.md#isolation-and-security) describes the main boundary.
This page explains the runtime and the host-side inputs that support it.

## Container runtime

[`compose.yaml`](../compose.yaml) runs one `dsh` service as UID/GID 10001. It makes
the root filesystem read-only, drops all Linux capabilities, and enables
`no-new-privileges`. `/home/agent` and `/workspace` use separate persistent named
volumes. `/tmp` and `/var/tmp` use writable tmpfs mounts with mode 1777,
`nosuid`, and `nodev`; the core allows execution there. The default limits are 2 GB
and 1 GB respectively, with 1 GB shared memory and a PID limit of 2048.

DSH runs with `DSH_PERMISSION_MODE=danger-full-access` inside this container.
Outbound network access remains available. The sandbox separates the agent from
host resources; it doesn't restrict what the agent sends to its provider or other
network services.

The [Git bridge](transfers.md) transfers objects and selected refs rather than
copying the host `.git` directory, its remotes, hooks, or credential helpers. The
container gets its own Git configuration. Host SSH/GitHub credentials and the
Docker socket aren't mounted.

## Authentication

[`start-dsh.sh`](../scripts/start-dsh.sh) starts DSH on container loopback port
3080 and a relay on 3081. Compose publishes the relay at `127.0.0.1:<port>` on the
host. DSH explicitly trusts that published localhost authority. Its browser token
protects web access; login URLs and logs containing them need the same care as
credentials.

For provider authentication, enter credentials in the UI, pass
`DEEPSEEK_API_KEY` in the launching process, or select a profile's `key_file`.
The low-level launcher also accepts `--key-file` before its command. A key file
contains only the API key, lives outside this public repository, and must be
readable by UID 10001. The launcher mounts it read-only at `/run/secrets/deepseek`;
startup reads it into `DEEPSEEK_API_KEY`, taking precedence over the launching
environment. The agent can read credentials supplied by either route.

DSH stores UI credentials and session settings in the persistent home volume.
Back that volume up separately if you need them; repository bundles cover Git
contents only.

## Plugins and host files

The [plugin contract](plugins.md) describes allowed Compose changes and runtime
hooks. Plugin Dockerfiles run during image builds, and Cordis patches can load
executable code; review both before selecting a plugin.

The launcher mounts the selected runtime directory read-only at `/opt/dsh-plugin`.
Everything in it is agent-readable, so keep it limited to intended configuration
and scripts. Put private plugins outside this repository; the launcher permits
in-repo plugins only under `examples/`. Ignored `.local/` is an accident guard,
not a supported private-plugin location.

The launcher loads explicit Compose files with an empty env file, avoiding a
repository `.env` and implicit Compose overrides. It writes generated Compose
settings and transfer staging files to OS temporary directories. The core Docker
build's [allowlist](../.dockerignore) includes only the Dockerfile and required
runtime helpers. A plugin's build context has its own contents and rules.

## Image contents

The [Dockerfile](../Dockerfile) uses Ubuntu 24.04 and copies Node 24 from the
official Node image. Ubuntu supplies glibc compatibility without project SDKs or
graphics libraries; add those through a plugin. Git, Python, ripgrep, and socat
support the runtime and transfer helpers.

DSH defaults to `0.1.5-rc.1`. Ubuntu/Node tags, apt packages, and npm transitive
dependencies can change between builds, so this isn't a fully reproducible lock.
The npm install explicitly allows install scripts for the listed runtime
dependencies. Review upstream changes when upgrading.
