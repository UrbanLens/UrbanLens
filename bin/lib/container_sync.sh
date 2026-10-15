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

# Hand-written source that must match in container: Python + templates.
# Excludes container-only artefacts (bytecode, static assets, __pycache__).
# Must stay literal: used unquoted via `eval`.
SOURCE_FILES="\\( -name '*.py' -o \\( -path '*/templates/*' -name '*.html' \\) \\) -not -path '*/__pycache__/*'"

# Paths under src/ not to overwrite, relative to src/. Empty by default.
# Set when target has volumes mounted inside tree; see bin/sync_app.sh.
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
        echo "    leaving $path alone"
    done
    # Extracted as appuser, who owns every directory written into here, with --no-same-owner so
    # the host's uid is not carried in. Neither is cosmetic: the sandbox services exec as appuser
    # and drop CAP_ALL, so root there has neither DAC override on an appuser-owned directory nor
    # CAP_CHOWN to put ownership back afterwards. Files land correctly owned instead.
    tar -C src -cf - "${tar_args[@]}" . | docker exec -i -u appuser "$container" tar -xf - --no-same-owner -C /app/src || return 1
    # bin/ is synced too: tests resolve checkers by path off the repo root, so
    # without this they error against the image's stale copy.
    docker cp bin/. "$container":/app/bin/ || return 1

    # Root deployment files: tests read them by path and they are baked into image, so sync all tracked ones.
    # /app is root-owned so extract as root; chown back where CAP_CHOWN allows,
    # else root-owned world-readable suffices for reading tests.
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
            gunicorn.conf.py pyproject.toml uv.lock .gitignore .env-sample .env.*-sample; do
            [ -e "$f" ] && docker cp "$f" "$container":/app/"$f"
        done
    fi
    # `docker cp` for the two below, not tar: the daemon writes these, so they land whatever the
    # container's own user could do, and neither is source the run imports - a stale copy of
    # either is a bad fixture, not the wrong code under test.
    docker cp sample_data/. "$container":/app/sample_data/ 2>/dev/null || true

    # Workflow files: baked into image and read by tests; sync whole dir,
    # not a list, to avoid a stale copy looking verified.
    docker cp .github/. "$container":/app/.github/ 2>/dev/null || true

    # Belt and braces: extracting as appuser already leaves the tree owned correctly, and this
    # only has an effect where something else in the container has not. Where CAP_CHOWN is
    # dropped it cannot run at all, so a failure here is not on its own a bad sync - parity is
    # what says whether the copy took.
    docker exec -u root "$container" chown -R appuser:appuser /app/src /app/bin 2>/dev/null || true

    prune_deleted_from "$container"
}

# Remove container source files deleted on host (`docker cp` only adds/overwrites).
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

# Fail unless container source matches host.
# Args:
#   $1: container name.
# Returns: 0 on match; 1 with diff on stderr otherwise.
verify_parity_with() {
    local container="$1"
    echo "==> verifying host and container agree"
    local host_list container_list
    host_list=$(mktemp)
    container_list=$(mktemp)

    (cd src && eval "find . $SOURCE_FILES" | sort) > "$host_list"
    docker exec "$container" sh -c "cd /app/src && find . $SOURCE_FILES | sort" > "$container_list"

    # No `trap ... RETURN`: it stays armed past this function and re-fires in wrappers after locals are gone.
    # Explicit rm avoids a `set -u` unbound-variable failure.
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
