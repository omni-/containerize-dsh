#!/bin/sh
set -eu
umask 077
if [ -n "${DEEPSEEK_API_KEY_FILE:-}" ]; then
    DEEPSEEK_API_KEY=$(cat "$DEEPSEEK_API_KEY_FILE")
    export DEEPSEEK_API_KEY
fi
mkdir -p "$DSH_HOME"
# Check all patches before any hook can change the workspace.
node /opt/dsh/plugin-runtime.mjs check
# Hooks run as the agent, once per start; long-running children belong in the background.
node /opt/dsh/plugin-runtime.mjs bootstrap
set -- dsh web
for runtime in $(node /opt/dsh/plugin-runtime.mjs paths); do
    if [ -f "$runtime/cordis.yml" ]; then
        set -- "$@" --patch "$runtime/cordis.yml"
    fi
done
# DSH listens on loopback. Docker publishes this relay only on host localhost.
socat TCP-LISTEN:3081,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:3080 &
exec "$@" --no-open --host 127.0.0.1 --port 3080 --trusted-host "127.0.0.1:${DSH_HOST_PORT:-11111}"
