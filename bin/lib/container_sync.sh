# shellcheck shell=bash
#
# Copying the working tree into a running container, correctly.
#
# Sourced by bin/run_tests.sh (test-runner) and bin/sync_app.sh (app). It lives
# here rather than in either caller because the sequence has three steps that
# are each individually easy to leave out, and leaving any of them out fails
# quietly:
#
#   1. `docker cp` preserves *source* ownership, so the copy must be chowned
#      back to the container's app user or it can no longer write what it
#      just received - a crash-looping build, not a warning.
#   2. `docker cp` only ever adds and overwrites. A file deleted on the host
#      stays in the container forever, so a deleted module still satisfies the
#      import that should have broken and a deleted template still renders.
#   3. The copy has to be checked rather than assumed, because a stale container
#      copy produces results that look like a normal pass.
#
# Callers set `set -euo pipefail` themselves; this file deliberately does not.

# A `find` expression naming the files whose container copy must match the host
# exactly - the hand-written source a run reads. Python, plus the template tree,
# which is rendered by name and which nothing generates.
#
# Deliberately not "every file": the container's tree legitimately holds
# artefacts the host does not (bytecode, collected/compressed static assets).
# `__pycache__` is excluded for the same reason. Used unquoted via `eval`,
# so it must stay a literal constant.
SOURCE_FILES="\\( -name '*.py' -o \\( -path '*/templates/*' -name '*.html' \\) \\) -not -path '*/__pycache__/*'"

# Paths under src/ the copy must not write into, relative to src/. Empty by
# default - the test runner mounts nothing under /app/src, so everything there
# is the container's own to overwrite. A caller that syncs into a container with
# volumes mounted inside the tree sets this; see bin/sync_app.sh for why that is
# not optional there.
SYNC_EXCLUDES=()

# Copy the working tree into a container and leave it owned by the app user.
#
# Args:
#   $1: container name.
sync_tree_into() {
    local container="$1"
    echo "==> syncing working tree into $container"
    # tar rather than `docker cp src/.`, only because docker cp cannot exclude a
    # path and some containers mount volumes inside the tree being copied. With
    # no excludes set the two are equivalent.
    local tar_args=() path
    for path in ${SYNC_EXCLUDES[@]+"${SYNC_EXCLUDES[@]}"}; do
        tar_args+=(--exclude="./$path")
        echo "    leaving $path alone (mounted volume)"
    done
    tar -C src -cf - "${tar_args[@]}" . | docker exec -i "$container" tar -xf - -C /app/src
    # bin/ is synced too: tests resolve checkers by path off the repo root, so
    # without this they error against the image's stale copy.
    docker cp bin/. "$container":/app/bin/

    # Deployment files, for the same reason: tests assert on the topology
    # (compose files, Dockerfile) by path, and they are baked into the image,
    # not bind-mounted. Sync every tracked root file rather than a list - a
    # list silently goes stale when a test starts reading a new one.
    local root_files
    root_files=$(git ls-files 2>/dev/null | grep -v "/" || true)
    if [ -n "$root_files" ]; then
        printf '%s\n' "$root_files" | tar -C . -T - -cf - | docker exec -i "$container" tar -xf - -C /app/
    else
        # No git (a tarball checkout, a stripped image): fall back to the files
        # tests are known to read today.
        local f
        for f in Dockerfile docker-compose.yml docker-compose.hot-reload.yml docker-entrypoint.sh \
            gunicorn.conf.py pyproject.toml uv.lock .gitignore .env-sample .env.ai-sample; do
            [ -e "$f" ] && docker cp "$f" "$container":/app/"$f"
        done
    fi
    docker cp sample_data/. "$container":/app/sample_data/ 2>/dev/null || true

    # Not optional - see the header. /app/src recursively, which is what covers
    # both the logs directory and the compiled frontend output underneath it.
    docker exec -u root "$container" chown -R appuser:appuser /app/src /app/bin

    prune_deleted_from "$container"
}

# Remove source files the container still has and the host no longer does.
#
# `docker cp` only ever adds and overwrites: a deleted module still satisfies
# the import that should have broken, and a deleted template still renders.
#
# Args:
#   $1: container name.
prune_deleted_from() {
    local container="$1"
    local host_list container_list stale
    host_list=$(mktemp); container_list=$(mktemp)
    (cd src && eval "find . $SOURCE_FILES" | sort) > "$host_list"
    docker exec "$container" sh -c "cd /app/src && find . $SOURCE_FILES | sort" > "$container_list"
    stale=$(comm -13 "$host_list" "$container_list" || true)
    rm -f "$host_list" "$container_list"
    if [ -n "$stale" ]; then
        echo "    pruning $(echo "$stale" | wc -l) stale source file(s) the host no longer has:"
        echo "$stale" | sed 's|^|      |'
        echo "$stale" | sed 's|^|/app/src/|' | tr '\n' '\0' | xargs -0 -r docker exec -u root "$container" rm -f
    fi
}

# Fail unless the container's source tree is identical to the host's.
#
# Args:
#   $1: container name.
#
# Returns:
#   0 when the trees match; 1 with a diff on stderr when they do not.
verify_parity_with() {
    local container="$1"
    echo "==> verifying host and container agree"
    local host_list container_list
    host_list=$(mktemp)
    container_list=$(mktemp)

    (cd src && eval "find . $SOURCE_FILES" | sort) > "$host_list"
    docker exec "$container" sh -c "cd /app/src && find . $SOURCE_FILES | sort" > "$container_list"

    # Cleaned up explicitly rather than with `trap ... RETURN`: a RETURN trap
    # stays armed past the function that set it, so calling this through a
    # one-line wrapper fires it a second time with the locals already gone and
    # `set -u` turns that into "host_list: unbound variable" from a line that
    # does not mention it.
    if ! diff -q "$host_list" "$container_list" >/dev/null; then
        echo "error: host and container differ - the run would use the wrong code:" >&2
        diff "$host_list" "$container_list" | head -20 >&2
        rm -f "$host_list" "$container_list"
        return 1
    fi

    # File lists matching is not enough: a stale *content* copy has the same
    # names. Compare a checksum of the tree, which is what actually gets run.
    local host_sum container_sum
    host_sum=$( (cd src && eval "find . $SOURCE_FILES -exec md5sum {} +") | sort -k2 | md5sum | cut -d' ' -f1)
    container_sum=$(docker exec "$container" sh -c "cd /app/src && find . $SOURCE_FILES -exec md5sum {} +" | sort -k2 | md5sum | cut -d' ' -f1)
    rm -f "$host_list" "$container_list"
    if [ "$host_sum" != "$container_sum" ]; then
        echo "error: host and container file lists match but contents differ - re-run the sync." >&2
        return 1
    fi
    echo "    tree matches ($host_sum)"
}
