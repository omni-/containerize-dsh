#!/bin/sh
set -eu
test -s /opt/example-image.txt
test "$(cat "$HOME/example-greeting.txt")" = "$EXAMPLE_GREETING"
test -r /opt/dsh-plugin/cordis.yml
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hello","arguments":{}}}' \
    | node /opt/dsh-plugin/server.mjs | grep -q 'Hello from the example MCP server'
printf 'Example image, environment, bootstrap and runtime mount passed.\n'
