#!/usr/bin/env bash
#
# Copy the working tree into a running app container.
#
# `/app/src` is baked into the image, not bind-mounted, so a running app
# container reflects whenever the image was last built. Syncing it by hand is
# what CLAUDE.local.md used to document, and the hand-typed form leaves out the
# step that matters: `docker cp` preserves *source* ownership, and the container
# runs as `appuser`. A copy from a host directory owned by anyone else takes
# away appuser's ability to write what it just received.
#
# That does not fail loudly. On 2026-08-14 it took out `src/urbanlens/logs/`,
# Django's logging config raised PermissionError, and the process died *before*
# binding port 8000 - while `docker exec` (which defaults to root) kept working,
# so every diagnostic and every pytest run in that session succeeded against a
# site that was down. On 2026-09-04 the same mechanism hit
# `dashboard/frontend/static/dashboard/js`: the entrypoint's `bun run build`
# could not remove its own output directory, `init.py` raised UnrecoverableError,
# and the container crash-looped - which also means `docker exec` gets
# "container is restarting", so the one command that repairs it is the one you
# cannot run. This script waits that out rather than making you time it.
#
# Compiled assets need the extra step --frontend performs. `collectstatic`
# populates a *volume* mounted at /app/src/urbanlens/frontend/static and shared
# with nginx; copying built output into the package directory never reaches it,
# so the site keeps serving whatever bundle was live at last boot even though
# the container's own copy of the file is current. Verified 2026-09-01, when a
# live-browser check of a same-day TS fix passed review and unit tests while
# exercising a three-hour-old bundle.
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

# A container crash-looping on a previous bad sync is the case this script most
# needs to handle, and it is the case `docker exec` refuses: the daemon answers
# "container is restarting" for every attempt that does not land inside the
# window where it is briefly up. Wait for one rather than reporting the refusal.
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
    # -u appuser, not the default. `docker exec` runs as root, which is the
    # single most misleading thing about working on this container: root can
    # write everything, so a build run that way succeeds, leaves root-owned
    # output, and hands the *next* boot the same EACCES this script exists to
    # prevent. Running as the account the entrypoint uses means a permission
    # problem fails here, loudly, instead of at 3am on a restart.
    echo "==> rebuilding the frontend in $CONTAINER"
    docker exec -u appuser "$CONTAINER" bun run build
    echo "==> collectstatic"
    docker exec -u appuser "$CONTAINER" /app/.venv/bin/python src/urbanlens/manage.py collectstatic --noinput
fi

if [ "$RESTART" -eq 1 ]; then
    echo "==> restarting $CONTAINER"
    docker restart "$CONTAINER" >/dev/null
    wait_for_exec
fi

echo "==> done"
