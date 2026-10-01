#!/bin/bash
set -e

# Fix root-owned volume mounts before dropping privileges.
#
# Fatal under root by design: a failed chown here surfaces loudly instead of as a silent Django log-handler failure later.
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
    # Retry once: files may vanish mid-traversal on live shared volumes.
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

# Daphne frame caps appended here rather than in docker-compose.yml.
if [ "$1" = "daphne" ] && [[ " $* " != *" --websocket-max-message-size "* ]]; then
    if [ ! -x /app/bin/websocket_frame_flags.sh ]; then
        echo "entrypoint: /app/bin/websocket_frame_flags.sh is missing; refusing to start daphne without its frame caps" >&2
        exit 1
    fi
    readarray -t ws_frame_flags < <(/app/bin/websocket_frame_flags.sh)
    set -- "$@" "${ws_frame_flags[@]}"
fi

# gosu needs dropped caps; `user:` services are already unprivileged and exec directly.
if [ "$(id -u)" = "0" ]; then
    exec gosu appuser "$@"
fi
exec "$@"
