#!/bin/bash
set -e

# Volume-mounted directories are owned by root at container start; fix them before
# dropping privileges so appuser can write logs, media, and compiled static assets.
#
# Best-effort, not fatal. The sandbox workers run `cap_drop: ALL` (no CAP_CHOWN)
# and already start as appuser, so every chown here fails for them - with `set -e`
# and no `|| true` that aborted the entrypoint before it ever exec'd celery, and
# the container crash-looped with nothing but "Operation not permitted" to show
# for it. Those services share these volumes with `app`, which does run as root
# and does fix the ownership; a container that cannot chown also does not need to.
for dir in \
    /var/log/urbanlens \
    /app/src/urbanlens/frontend/static \
    /app/src/urbanlens/media; do
    mkdir -p "$dir" 2>/dev/null || true
    chown -R appuser:appuser "$dir" 2>/dev/null || true
done

# Decided here, not in the image, so one image can run as either environment.
# staging/production bake the source in and should keep the bytecode they
# compile - discarding it makes every worker recompile whatever the build-time
# compileall missed. Local and development bind-mount the source from the host,
# where __pycache__ would litter the developer's checkout.
case "${UL_ENVIRONMENT:-production}" in
    staging | production)
        unset PYTHONDONTWRITEBYTECODE
        ;;
    *)
        export PYTHONDONTWRITEBYTECODE=1
        ;;
esac

# gosu needs CAP_SETUID/CAP_SETGID, which `cap_drop: ALL` also removes - a
# service that declares `user:` is already unprivileged and must exec directly.
if [ "$(id -u)" = "0" ]; then
    exec gosu appuser "$@"
fi
exec "$@"
