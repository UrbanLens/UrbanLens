#!/bin/bash
set -e

# Volume-mounted directories are owned by root at container start; fix them before
# dropping privileges so appuser can write logs, media, compiled static assets, and
# database backups.
#
# Still fatal when running as root, deliberately. A root chown that fails means
# something is genuinely wrong (a read-only volume, ownership a previous run
# left broken), and the alternative to dying loudly here is dying silently
# later: Django's file log handler raises PermissionError -> "Unable to
# configure handler 'file'", the process exits before binding its port, and
# `docker logs` shows nothing at all. That failure is written up in
# docs/PROBLEMS.md; this exit code is what makes it visible.
for dir in \
    /var/log/urbanlens \
    /app/src/urbanlens/frontend/static \
    /app/src/urbanlens/media \
    /app/src/backups; do
    if [ "$(id -u)" != "0" ]; then
        mkdir -p "$dir" 2>/dev/null || true
        chown -R appuser:appuser "$dir" 2>/dev/null || true
        continue
    fi

    mkdir -p "$dir"
    # Retried once before giving up, because `chown -R` exits non-zero when a
    # file vanishes mid-traversal - and these volumes are shared with containers
    # that are live and deleting files (delete_stored_file, the preview-source
    # sweep).
    if ! chown -R appuser:appuser "$dir"; then
        echo "entrypoint: chown of $dir failed, retrying once" >&2
        chown -R appuser:appuser "$dir"
    fi
done

# Every step here is non-fatal
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
    if mkdir -p "${PROMETHEUS_MULTIPROC_DIR}" 2>/dev/null; then
        rm -f "${PROMETHEUS_MULTIPROC_DIR}"/*.db 2>/dev/null || true
        if [ "$(id -u)" = "0" ]; then
            chown -R appuser:appuser "${PROMETHEUS_MULTIPROC_DIR}" 2>/dev/null ||
                echo "entrypoint: chown of ${PROMETHEUS_MULTIPROC_DIR} failed; metrics may be unavailable" >&2
        fi
    else
        echo "entrypoint: could not create ${PROMETHEUS_MULTIPROC_DIR}; metrics will be unavailable" >&2
    fi
fi

# Decided here, not in the image, so one image can run as either environment.
case "${UL_ENVIRONMENT:-production}" in
    staging | production)
        unset PYTHONDONTWRITEBYTECODE
        ;;
    *)
        export PYTHONDONTWRITEBYTECODE=1
        ;;
esac

# Daphne's inbound frame caps are appended here rather than written into
# docker-compose.yml
if [ "$1" = "daphne" ] && [[ " $* " != *" --websocket-max-message-size "* ]]; then
    if [ ! -x /app/bin/websocket_frame_flags.sh ]; then
        echo "entrypoint: /app/bin/websocket_frame_flags.sh is missing; refusing to start daphne without its frame caps" >&2
        exit 1
    fi
    readarray -t ws_frame_flags < <(/app/bin/websocket_frame_flags.sh)
    set -- "$@" "${ws_frame_flags[@]}"
fi

# gosu needs CAP_SETUID/CAP_SETGID, which `cap_drop: ALL` also removes - a
# service that declares `user:` is already unprivileged and must exec directly.
if [ "$(id -u)" = "0" ]; then
    exec gosu appuser "$@"
fi
exec "$@"
