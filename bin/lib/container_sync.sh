# shellcheck shell=bash
#
# Copying the working tree into a running container, correctly.
#
# Sourced by bin/run_tests.sh (test-runner) and bin/sync_app.sh (app). It lives
# here rather than in either caller because the sequence has three steps that
# are each individually easy to leave out, and leaving any of them out fails
# quietly:
#
#   1. `docker cp` preserves *source* ownership. The host tree is owned by
#      whichever account last wrote to it - `apps` (uid 568) for a checkout,
#      `claude` (uid 3300) for a directory an agent's build created - and the
#      container's app runs as `appuser` (uid 1001). Without the chown, appuser
#      can no longer write what it just received. That is not a warning: on
#      2026-09-04 the development_main app container crash-looped through
#      `bun run build` because `docker cp` had handed
#      dashboard/frontend/static/dashboard/js to uid 3300 and the build's own
#      `rm -rf` of its output directory got EACCES. The same mechanism took the
#      logs directory out on 2026-08-14 (see docs/archive/PROBLEMS-ARCHIVE.md).
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
# artefacts the host does not (compiled bytecode, collected and compressed
# static assets), and an early version that pruned every extra file removed
# ~19,700 of them. Nothing broke, because those regenerate - but deleting build
# output is not this script's job. `__pycache__` is excluded for the same
# reason. Used unquoted via `eval`, so it must stay a literal constant.
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
    # bin/ is synced too. It was dropped when bin/opslib and the ops-tooling
    # tests that reached it by path moved to the separate `infrastructure` repo,
    # on the grounds that nothing under tests/ read it anymore - but three
    # modules do (test_template_comments.py, test_run_codeql.py,
    # test_ops_tooling_contract.py), each resolving a checker by path off the
    # repo root. Without this they error at setup with FileNotFoundError against
    # whatever the image was last built with, which reads as a broken test
    # rather than as missing coverage.
    docker cp bin/. "$container":/app/bin/

    # Deployment files, for the same reason bin/ is here: a growing set of tests
    # asserts on the topology rather than on Python (test_ai_isolation,
    # test_sandbox_isolation, test_metrics_endpoint), resolving these by path off
    # the repo root. They are baked into the image, not bind-mounted, so without
    # this they are read at whatever the image was last built with.
    #
    # That failure is worse than a plain stale-code one, because the sync still
    # prints "tree matches" and the run still looks verified: on 2026-09-03 a
    # runner whose image predated the ai-inference work failed all 42
    # ComposeTopologyTests against a compose file with no ai-inference in it,
    # which reads as "the branch broke the sandbox topology" rather than as
    # "this file was never synced".
    #
    # Dotfiles are listed individually because `docker cp` on a directory does
    # not glob them, and .gitignore/.env*-sample are read by those same tests.
    local f
    for f in docker-compose.yml docker-entrypoint.sh gunicorn.conf.py \
        pyproject.toml uv.lock .gitignore .env-sample .env.ai-sample; do
        [ -e "$f" ] && docker cp "$f" "$container":/app/"$f"
    done
    docker cp sample_data/. "$container":/app/sample_data/ 2>/dev/null || true

    # Not optional - see the header. /app/src recursively, which is what covers
    # both the logs directory and the compiled frontend output underneath it.
    docker exec -u root "$container" chown -R appuser:appuser /app/src /app/bin

    prune_deleted_from "$container"
}

# Remove source files the container still has and the host no longer does.
#
# `docker cp` only ever adds and overwrites. That is not cosmetic: a scratch
# test file deleted after use is still collected there, a module deleted in a
# refactor still satisfies the import that should have broken, and a template
# deleted in one still resolves by name for anything that renders it. All three
# were real: `pages/memories/photos.html`, deleted on 2026-08-30 when
# Memories > Photos moved to the Vault, was still being served four days later.
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
