#!/usr/bin/env bash
#
# Copy the working tree into a running app container.
#
# /app/src is baked into the image; `docker cp` preserves source ownership, so chown back to appuser.
#
# Waits out a crash-looping container and rebuilds frontend assets on request.
#
# Usage:
#   bin/sync_app.sh                    # sync source, chown, prune, verify
#   bin/sync_app.sh --frontend         # also rebuild SCSS/TS and collectstatic
#   bin/sync_app.sh --restart          # also restart the container afterwards
#
# Environment:
#   UL_APP_CONTAINER   app container name (default urbanlens_development_main_app)
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# shellcheck source=bin/lib/container_sync.sh
. bin/lib/container_sync.sh

CONTAINER="${UL_APP_CONTAINER:-urbanlens_development_main_app}"
FRONTEND=0
RESTART=0

# Volumes inside the tree hold live state, not Python/templates, so exclude them from the copy.
SYNC_EXCLUDES=(urbanlens/frontend/static urbanlens/media backups)

for arg in "$@"; do
    case "$arg" in
        --frontend) FRONTEND=1 ;;
        --restart) RESTART=1 ;;
        *) echo "error: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_APP_CONTAINER or start the stack." >&2
    exit 2
fi

# Crash-looping containers only accept exec in brief windows; wait for one.
wait_for_exec() {
    local attempt
    for attempt in $(seq 1 60); do
        if docker exec "$CONTAINER" true 2>/dev/null; then
            [ "$attempt" -gt 1 ] && echo "    caught it on attempt $attempt"
            return 0
        fi
        [ "$attempt" -eq 1 ] && echo "==> '$CONTAINER' is not accepting exec (restarting?) - waiting for a window"
        sleep 2
    done
    echo "error: '$CONTAINER' never accepted a command. Check 'docker logs $CONTAINER'." >&2
    return 1
}

wait_for_exec
sync_tree_into "$CONTAINER"
verify_parity_with "$CONTAINER"

if [ "$FRONTEND" -eq 1 ]; then
    # Build as appuser; root-owned output breaks the next boot.
    echo "==> rebuilding the frontend in $CONTAINER"
    docker exec -u appuser "$CONTAINER" bun run build
    # `bun run build` skips SCSS, so compile it separately.
    echo "==> compiling SCSS"
    docker exec -u appuser "$CONTAINER" bun run sass
    echo "==> collectstatic"
    docker exec -u appuser "$CONTAINER" /app/.venv/bin/python src/urbanlens/manage.py collectstatic --noinput
fi

if [ "$RESTART" -eq 1 ]; then
    echo "==> restarting $CONTAINER"
    docker restart "$CONTAINER" >/dev/null
    wait_for_exec
fi

echo "==> done"
