#!/bin/bash
set -e

# Volume-mounted directories are owned by root at container start; fix them before
# dropping privileges so appuser can write logs, media, and compiled static assets.
#
# Tolerated only for a container that is already unprivileged. The sandbox
# workers run `cap_drop: ALL` (no CAP_CHOWN) and declare `user: 1001:1001`, so
# every chown here fails for them - with `set -e` that aborted the entrypoint
# before it ever exec'd celery, and the container crash-looped with nothing but
# "Operation not permitted" to show for it. They share these volumes with `app`,
# which does run as root and does fix the ownership.
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
    /app/src/urbanlens/media; do
    if [ "$(id -u)" != "0" ]; then
        mkdir -p "$dir" 2>/dev/null || true
        chown -R appuser:appuser "$dir" 2>/dev/null || true
        continue
    fi

    mkdir -p "$dir"
    # Retried once before giving up, because `chown -R` exits non-zero when a
    # file vanishes mid-traversal - and these volumes are shared with containers
    # that are live and deleting files (delete_stored_file, the preview-source
    # sweep). A restart during ordinary traffic would otherwise crash-loop on a
    # race. A real permission problem fails both attempts and still exits.
    if ! chown -R appuser:appuser "$dir"; then
        echo "entrypoint: chown of $dir failed, retrying once" >&2
        chown -R appuser:appuser "$dir"
    fi
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
