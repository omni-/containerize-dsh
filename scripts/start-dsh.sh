#!/bin/sh
set -eu
umask 077
if [ -n "${DEEPSEEK_API_KEY_FILE:-}" ]; then
    DEEPSEEK_API_KEY=$(cat "$DEEPSEEK_API_KEY_FILE")
    export DEEPSEEK_API_KEY
fi
mkdir -p "$DSH_HOME"
mode=${1:-web}
# Check all patches before any hook can change the workspace.
if [ "$mode" = headless ]; then
    node /opt/dsh/plugin-runtime.mjs check >&2
else
    node /opt/dsh/plugin-runtime.mjs check
fi
# Hooks run as the agent, once per start; long-running children belong in the background.
if [ "$mode" = headless ]; then
    node /opt/dsh/plugin-runtime.mjs bootstrap >&2
    set -- dsh headless
else
    node /opt/dsh/plugin-runtime.mjs bootstrap
    set -- dsh web
fi
for runtime in $(node /opt/dsh/plugin-runtime.mjs paths); do
    if [ -f "$runtime/cordis.yml" ]; then
        set -- "$@" --patch "$runtime/cordis.yml"
    fi
done
if [ "$mode" = headless ]; then
    if [ -n "${DSH_HEADLESS_MODEL:-}${DSH_HEADLESS_EFFORT:-}" ]; then
        node /opt/dsh/plugin-runtime.mjs headless-model
        set -- "$@" --patch /tmp/dsh-headless-model.json
    fi
    exec "$@" --json --permission-mode danger-full-access --approval-policy never -
fi
# DSH listens on loopback. Docker publishes this relay only on host localhost.
socat TCP-LISTEN:3081,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:3080 &
exec "$@" --no-open --host 127.0.0.1 --port 3080 --trusted-host "127.0.0.1:${DSH_HOST_PORT:-11111}"
