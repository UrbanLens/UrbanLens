#!/bin/bash
set -e

# Volume-mounted directories are owned by root at container start; fix them before
# dropping privileges so appuser can write logs, media, and compiled static assets.
for dir in \
    /var/log/urbanlens \
    /app/src/urbanlens/frontend/static \
    /app/src/urbanlens/media; do
    mkdir -p "$dir"
    chown -R appuser:appuser "$dir"
done

exec gosu appuser "$@"
