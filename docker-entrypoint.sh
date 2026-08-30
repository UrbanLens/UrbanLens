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

exec gosu appuser "$@"
