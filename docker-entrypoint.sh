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

# Bytecode caching is environment-dependent, so it is decided here rather than
# baked into the image - one image can be run as either.
#
# staging/production: leave PYTHONDONTWRITEBYTECODE unset so Python keeps what
# it compiles. The source is baked into the image and never changes, and
# discarding bytecode means every gunicorn worker recompiles anything the
# build-time compileall missed on its first import. That cost is not
# theoretical: a cold worker spent 5.2s of a 12.3s first request in
# builtins.compile before the build started precompiling.
#
# everything else: set it. Local and development bind-mount the source from the
# host, where writing __pycache__ into the developer's own checkout is litter
# that git then has to ignore.
case "${UL_ENVIRONMENT:-production}" in
    staging | production)
        unset PYTHONDONTWRITEBYTECODE
        ;;
    *)
        export PYTHONDONTWRITEBYTECODE=1
        ;;
esac

exec gosu appuser "$@"
