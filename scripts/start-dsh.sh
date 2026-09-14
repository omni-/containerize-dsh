#!/bin/sh
set -eu
umask 077
if [ -n "${DEEPSEEK_API_KEY_FILE:-}" ]; then
    DEEPSEEK_API_KEY=$(cat "$DEEPSEEK_API_KEY_FILE")
    export DEEPSEEK_API_KEY
fi
mkdir -p "$DSH_HOME"
# Hooks run as the agent, once per start; long-running children belong in the background.
if [ -f /opt/dsh-plugin/bootstrap.sh ]; then
    /bin/sh /opt/dsh-plugin/bootstrap.sh
fi
set -- dsh web
if [ -f /opt/dsh-plugin/cordis.yml ]; then
    set -- "$@" --patch /opt/dsh-plugin/cordis.yml
fi
# DSH listens on loopback. Docker publishes this relay only on host localhost.
socat TCP-LISTEN:3081,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:3080 &
exec "$@" --no-open --port 3080 --trusted-host "127.0.0.1:${DSH_HOST_PORT:-11111}"
