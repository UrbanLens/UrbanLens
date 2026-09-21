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
    # Callers test this function so that one failed container does not abort the others, and
    # `set -e` is suspended inside a condition - re-setting it here would not restore it. Every
    # step that must not be skipped therefore returns explicitly.
    echo "==> syncing working tree into $container"
    # tar rather than `docker cp src/.`, only because docker cp cannot exclude a
    # path and some containers mount volumes inside the tree being copied. With
    # no excludes set the two are equivalent.
    local tar_args=() path
    for path in ${SYNC_EXCLUDES[@]+"${SYNC_EXCLUDES[@]}"}; do
        tar_args+=(--exclude="./$path")
        echo "    leaving $path alone (mounted volume)"
    done
    # Extracted as appuser, who owns every directory written into here, with --no-same-owner so
    # the host's uid is not carried in. Neither is cosmetic: the sandbox services exec as appuser
    # and drop CAP_ALL, so root there has neither DAC override on an appuser-owned directory nor
    # CAP_CHOWN to put ownership back afterwards. Files land correctly owned instead.
    tar -C src -cf - "${tar_args[@]}" . | docker exec -i -u appuser "$container" tar -xf - --no-same-owner -C /app/src || return 1
    # bin/ is synced too: tests resolve checkers by path off the repo root, so
    # without this they error against the image's stale copy.
    docker cp bin/. "$container":/app/bin/ || return 1

    # Deployment files, for the same reason: tests assert on the topology
    # (compose files, Dockerfile) by path, and they are baked into the image,
    # not bind-mounted. Sync every tracked root file rather than a list - a
    # list silently goes stale when a test starts reading a new one.
    #
    # These sit directly in /app, which is root's, so this is the one step appuser cannot do.
    # Ownership is then restored where the container still has CAP_CHOWN; where it does not they
    # stay root-owned and world-readable, which is all a test that reads them needs.
    local root_files
    root_files=$(git ls-files 2>/dev/null | grep -v "/" || true)
    if [ -n "$root_files" ]; then
        printf '%s\n' "$root_files" | tar -C . -T - -cf - | docker exec -i -u root "$container" tar -xf - --no-same-owner -C /app/ || return 1
        printf '%s\n' "$root_files" | sed 's|^|/app/|' | tr '\n' '\0' \
            | xargs -0 -r docker exec -u root "$container" chown appuser:appuser 2>/dev/null || true
    else
        # No git (a tarball checkout, a stripped image): fall back to the files
        # tests are known to read today.
        local f
        for f in Dockerfile docker-compose.yml docker-compose.hot-reload.yml docker-entrypoint.sh \
            gunicorn.conf.py pyproject.toml uv.lock .gitignore .env-sample .env.ai-sample; do
            [ -e "$f" ] && docker cp "$f" "$container":/app/"$f"
        done
    fi
    # `docker cp` for the two below, not tar: the daemon writes these, so they land whatever the
    # container's own user could do, and neither is source the run imports - a stale copy of
    # either is a bad fixture, not the wrong code under test.
    docker cp sample_data/. "$container":/app/sample_data/ 2>/dev/null || true

    # The workflow files, for the same reason and by the same argument as the
    # root files above: they are baked into the image, a test reads them
    # (test_typecheck_dependencies_are_installed), and a stale copy makes the
    # run look verified while asserting against whatever the image was built
    # with. Whole directory rather than a list, because picking the file a
    # future test will read is the judgement that keeps being got wrong.
    docker cp .github/. "$container":/app/.github/ 2>/dev/null || true

    # Belt and braces: extracting as appuser already leaves the tree owned correctly, and this
    # only has an effect where something else in the container has not. Where CAP_CHOWN is
    # dropped it cannot run at all, so a failure here is not on its own a bad sync - parity is
    # what says whether the copy took.
    docker exec -u root "$container" chown -R appuser:appuser /app/src /app/bin 2>/dev/null || true

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
        echo "$stale" | sed 's|^|/app/src/|' | tr '\n' '\0' | xargs -0 -r docker exec -u appuser "$container" rm -f
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
