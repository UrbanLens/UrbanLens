#!/usr/bin/env bash
#
# Copy the working tree into the running app containers.
#
# /app/src is baked into the image; `docker cp` preserves source ownership, so chown back to appuser.
#
# Every service built from the app image runs that baked copy - the WSGI app, daphne, the Celery
# workers and beat - so syncing only the one named here leaves the rest running whatever the image
# was built from. They are found by image and compose project and synced too.
#
# Waits out a crash-looping container and rebuilds frontend assets on request.
#
# Usage:
#   bin/sync_app.sh                    # sync source, chown, prune, verify
#   bin/sync_app.sh --frontend         # also rebuild SCSS/TS and collectstatic
#   bin/sync_app.sh --restart          # also restart the containers afterwards
#   bin/sync_app.sh --only             # sync just UL_APP_CONTAINER, not its siblings
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
ONLY=0

# Volumes inside the tree hold live state, not Python/templates, so exclude them from the copy.
SYNC_EXCLUDES=(urbanlens/frontend/static urbanlens/media backups)

for arg in "$@"; do
    case "$arg" in
        --frontend) FRONTEND=1 ;;
        --restart) RESTART=1 ;;
        --only) ONLY=1 ;;
        *) echo "error: unknown argument '$arg'" >&2; exit 2 ;;
    esac
done

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_APP_CONTAINER or start the stack." >&2
    exit 2
fi

# Crash-looping containers only accept exec in brief windows; wait for one.
wait_for_exec() {
    local container="$1" attempt
    for attempt in $(seq 1 60); do
        if docker exec "$container" true 2>/dev/null; then
            [ "$attempt" -gt 1 ] && echo "    caught it on attempt $attempt"
            return 0
        fi
        [ "$attempt" -eq 1 ] && echo "==> '$container' is not accepting exec (restarting?) - waiting for a window"
        sleep 2
    done
    echo "error: '$container' never accepted a command. Check 'docker logs $container'." >&2
    return 1
}

# Every other running container in the same compose project that carries a copy of the source.
#
# Found by looking for the app image's own interpreter rather than by image id: each app-family
# service is its own compose service with its own build, so daphne and the workers run different
# images from the same tree. nginx mounts `/app` too, which is why the test is the interpreter.
siblings_of() {
    local container="$1" project name
    project=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container")
    # Without a project label the filter below matches every unlabelled container on the host.
    [ -n "$project" ] || return 0
    for name in $(docker ps --format '{{.Names}}' --filter "label=com.docker.compose.project=$project"); do
        [ "$name" = "$container" ] && continue
        docker exec "$name" test -x /app/.venv/bin/python 2>/dev/null && echo "$name"
    done
}

TARGETS=("$CONTAINER")
if [ "$ONLY" -eq 0 ]; then
    while read -r sibling; do
        [ -n "$sibling" ] && TARGETS+=("$sibling")
    done < <(siblings_of "$CONTAINER")
fi
echo "==> syncing ${#TARGETS[@]} container(s): ${TARGETS[*]}"

for target in "${TARGETS[@]}"; do
    wait_for_exec "$target"
    sync_tree_into "$target"
    verify_parity_with "$target"
done

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
    for target in "${TARGETS[@]}"; do
        echo "==> restarting $target"
        docker restart "$target" >/dev/null
    done
    for target in "${TARGETS[@]}"; do
        wait_for_exec "$target"
    done
fi

echo "==> done"
