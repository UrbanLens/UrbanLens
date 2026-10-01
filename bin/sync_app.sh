#!/usr/bin/env bash
#
# Copy the working tree into the running app containers.
#
# /app/src is baked into the image; `docker cp` preserves source ownership, so chown back to appuser.
#
# Every service built from the app image runs that baked copy - the WSGI app, daphne, the Celery
# workers and beat - so syncing only the one named here leaves the rest running whatever the image
# was built from. They are found by compose project and synced too, by asking each container
# whether it carries the app image's interpreter - so a sibling that is crash-looping is skipped
# rather than synced, and the printed target list is what says which ones were reached.
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

# Volumes and host logs inside the tree hold live state, not Python/templates, so exclude them from the copy.
SYNC_EXCLUDES=(urbanlens/frontend/static urbanlens/media backups urbanlens/logs)

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
#
# Where there is a healthcheck, wait for it rather than for the first window: a container part way
# through its entrypoint accepts exec while its workers are still spawning, and syncing into that
# swaps the tree out from under a worker that then reads a file it does not own yet. The chown
# lands a moment later and the container is already crash-looping, which reads as a bad sync
# rather than a badly timed one. Unhealthy after the wait is still synced - that is the case the
# brief-window handling exists for.
wait_for_exec() {
    local container="$1" attempt health
    for attempt in $(seq 1 60); do
        if docker exec "$container" true 2>/dev/null; then
            [ "$attempt" -gt 1 ] && echo "    caught it on attempt $attempt"
            health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null)
            [ "$health" = "starting" ] || return 0
            [ "$attempt" -eq 1 ] && echo "==> '$container' is still starting - waiting rather than syncing under its workers"
        elif [ "$attempt" -eq 1 ]; then
            echo "==> '$container' is not accepting exec (restarting?) - waiting for a window"
        fi
        sleep 2
    done
    if docker exec "$container" true 2>/dev/null; then
        echo "    '$container' never reported healthy; syncing anyway"
        return 0
    fi
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

# Every target is attempted even after one fails. Aborting on the first leaves the rest running
# whatever the image was built from, which is the failure this script exists to prevent, and the
# printed target list then names containers it never reached.
FAILED=()
SKIPPED=()
for target in "${TARGETS[@]}"; do
    # A read-only root filesystem cannot be synced at all. That is a property of the service, not
    # a fault, but it does mean the container keeps running its baked copy - so it is named here
    # rather than passed over, because "synced" would otherwise cover a container that was not.
    if [ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$target")" = "true" ]; then
        echo "==> skipping $target (read-only root filesystem - it runs its baked copy)"
        SKIPPED+=("$target")
        continue
    fi
    if wait_for_exec "$target" && sync_tree_into "$target" && verify_parity_with "$target"; then
        continue
    fi
    echo "error: sync into '$target' failed - it is still running its baked copy." >&2
    FAILED+=("$target")
done

if [ "${#SKIPPED[@]}" -gt 0 ]; then
    echo "==> ${#SKIPPED[@]} read-only container(s) left on their baked copy: ${SKIPPED[*]}"
fi

if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "error: ${#FAILED[@]} of ${#TARGETS[@]} container(s) not synced: ${FAILED[*]}" >&2
    exit 1
fi

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

# Containers joined with `network_mode: container:<id>` stay in the old namespace when that host
# restarts (test-db behind test-runner), and only a recreate brings them back.
hosts_network_for_others() {
    local id
    id=$(docker inspect --format '{{.Id}}' "$1")
    docker ps -q --no-trunc | xargs -r docker inspect --format '{{.HostConfig.NetworkMode}}' | grep -qx "container:$id"
}

if [ "$RESTART" -eq 1 ]; then
    RESTARTED=()
    for target in "${TARGETS[@]}"; do
        if hosts_network_for_others "$target"; then
            echo "==> not restarting $target (other containers share its network namespace)"
            continue
        fi
        echo "==> restarting $target"
        docker restart "$target" >/dev/null
        RESTARTED+=("$target")
    done
    for target in "${RESTARTED[@]}"; do
        wait_for_exec "$target"
    done
fi

echo "==> done"
