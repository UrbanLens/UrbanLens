#!/usr/bin/env bash
#
# Run pytest inside the test container, with the sync this repo requires.
#
# Syncs all of src/, not just src/urbanlens: src/bin/init.py is the container's
# entrypoint and is real, imported code. Syncing only the package meant a change
# there was tested against the image's stale copy - which is exactly the failure
# this script exists to prevent, and it had this bug until 2026-08-17.
#
# The container's /app/src is baked into the image, not bind-mounted, so it
# reflects whenever the image was last built. Every run therefore has to copy
# the working tree in first - and `docker cp` preserves *source* ownership, so
# the copy must be chowned back to the container's app user or Django's logging
# config raises PermissionError and the process dies before binding anything.
#
# Getting that sequence wrong is not loud. A file restored on the host but not
# re-copied leaves the container running the previous version, and the suite
# reports on code that is not the code under test - which is how the one red
# consolidation run of the 2026-08-17 audit happened. This script exists so the
# sequence cannot be typed wrong, and verifies parity afterwards rather than
# assuming the copy landed.
#
# Usage:
#   bin/run_tests.sh [pytest args...]           # sync, then run
#   bin/run_tests.sh --no-sync [pytest args...] # reuse the container as-is
#   bin/run_tests.sh --verify-only              # just compare host and container
#   bin/run_tests.sh --allow-drift ...          # run despite drift, on purpose
#   bin/run_tests.sh --fast [pytest args...]    # reuse a persistent database
#   bin/run_tests.sh --fresh-db [pytest args...]# rebuild it (needed after a migration)
#   bin/run_tests.sh --parallel[=N] [args...]   # N xdist workers (default: auto)
#   bin/run_tests.sh --shuffle [pytest args...] # randomise test order
#
# --fast is worth knowing about. A unique database per run is what keeps
# parallel sessions from colliding, but building one costs about three minutes,
# which dwarfs the tests themselves: the consensus field-scope file takes 188
# seconds cold and 3.5 seconds against a database that already exists. For a
# tight edit-run loop, or anything that runs the same tests hundreds of times
# (mutation testing), reuse the database and rebuild it when the schema moves.
#
# --parallel is the other half of that arithmetic, and it cuts the opposite way:
# pytest-django gives every xdist worker its own database (`..._gw0`, `_gw1`,
# ...), so N workers means N database builds before any test runs. It pays off
# on a large selection or against --fast, and loses badly on a single file.
# Combined with --fast each worker reuses its own database, which is the
# configuration worth having. Deliberately not the default: this multiplies
# concurrent load on Postgres, which is exactly what has been observed to take
# the local instance down (it shows up as mass "ERROR at setup" in files that
# have nothing to do with each other).
#
# --shuffle turns on pytest-randomly, which is installed but disabled in
# `addopts`. Shuffling found no order dependence when it was probed across three
# seeds, but only over a subset - so it is opt-in until a full shuffled run has
# been green, and a failure under it is worth reproducing with the seed pytest
# prints before assuming the plugin is at fault.
#
# Environment:
#   UL_TEST_CONTAINER   test-runner container name (default urbanlens_development_main_test_runner)
#   UL_TEST_DB_NAME     test database name; a unique one is generated when unset,
#                       because parallel runs collide otherwise - and the test
#                       channel-layer prefix is derived from it, so websocket
#                       tests in overlapping runs would consume each other's
#                       messages without it.
set -euo pipefail

CONTAINER="${UL_TEST_CONTAINER:-urbanlens_development_main_test_runner}"
SYNC=1
VERIFY_ONLY=0
FAST=0
FRESH_DB=0
# Verifying a fix by breaking it means deliberately editing the container's copy
# and expecting the tests to fail. That is drift on purpose, so it needs a way
# past the guard - named so it cannot be reached by accident or by habit.
ALLOW_DRIFT=0
PARALLEL=""
SHUFFLE=0

args=()
for arg in "$@"; do
    case "$arg" in
        --no-sync) SYNC=0 ;;
        --allow-drift) ALLOW_DRIFT=1 ;;
        --fast) FAST=1 ;;
        --fresh-db) FAST=1; FRESH_DB=1 ;;
        --verify-only) VERIFY_ONLY=1 ;;
        --parallel) PARALLEL="auto" ;;
        --parallel=*) PARALLEL="${arg#*=}" ;;
        --shuffle) SHUFFLE=1 ;;
        *) args+=("$arg") ;;
    esac
done

if [ -n "$PARALLEL" ]; then
    # --dist loadfile keeps each file's tests on one worker. Several suites here
    # build expensive per-class state, and splitting a class across workers pays
    # that cost once per worker instead of once.
    args=(-n "$PARALLEL" --dist loadfile "${args[@]}")
fi
if [ "$SHUFFLE" -eq 1 ]; then
    # Undoes the `-p no:randomly` in pyproject's addopts; a later -p wins.
    args=(-p randomly "${args[@]}")
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_TEST_CONTAINER or start the stack." >&2
    exit 2
fi

sync_tree() {
    echo "==> syncing working tree into $CONTAINER"
    docker cp src/. "$CONTAINER":/app/src/
    # bin/ is not synced: nothing under tests/ imports from it anymore now that
    # bin/opslib and the ops-tooling tests that reached it by path have moved to
    # the separate `infrastructure` repo (see docs/PROBLEMS.md's history of this
    # sync, and that repo's tests/test_ops_tooling.py for where those tests live
    # now). If a future test starts importing something under bin/ again, restore
    # this the same way src/ is handled below.
    # Not optional: docker cp preserves host ownership, and the app runs as appuser.
    docker exec -u root "$CONTAINER" chown -R appuser:appuser /app/src

    # `docker cp` only ever adds and overwrites - a Python file deleted on the host
    # stays in the container forever. That is not cosmetic: a scratch test file
    # deleted after use is still collected there, and a module deleted in a refactor
    # still satisfies the import that should have broken.
    #
    # Only `.py` files are pruned, and never `__pycache__`. The container's tree
    # legitimately holds artefacts the host does not - compiled bytecode, collected
    # and compressed static assets - and an early version of this that pruned every
    # extra file removed ~19,700 of them. Nothing broke, because those regenerate,
    # but deleting build output is not this script's job.
    local host_list container_list
    host_list=$(mktemp); container_list=$(mktemp)
    (cd src && find . -name '*.py' -not -path '*/__pycache__/*' | sort) > "$host_list"
    docker exec "$CONTAINER" sh -c "cd /app/src && find . -name '*.py' -not -path '*/__pycache__/*' | sort" > "$container_list"
    local stale
    stale=$(comm -13 "$host_list" "$container_list" || true)
    rm -f "$host_list" "$container_list"
    if [ -n "$stale" ]; then
        echo "    pruning $(echo "$stale" | wc -l) stale .py file(s) the host no longer has:"
        echo "$stale" | sed 's|^|      |'
        echo "$stale" | sed 's|^|/app/src/|' | tr '\n' '\0' | xargs -0 -r docker exec -u root "$CONTAINER" rm -f
    fi
}

verify_parity() {
    echo "==> verifying host and container agree"
    local host_list container_list
    host_list=$(mktemp)
    container_list=$(mktemp)
    trap 'rm -f "$host_list" "$container_list"' RETURN

    (cd src && find . -name '*.py' | sort) > "$host_list"
    docker exec "$CONTAINER" sh -c "cd /app/src && find . -name '*.py' | sort" > "$container_list"

    if ! diff -q "$host_list" "$container_list" >/dev/null; then
        echo "error: host and container differ - the run would test the wrong code:" >&2
        diff "$host_list" "$container_list" | head -20 >&2
        return 1
    fi

    # File lists matching is not enough: a stale *content* copy has the same
    # names. Compare a checksum of the tree, which is what actually gets run.
    local host_sum container_sum
    host_sum=$( (cd src && find . -name '*.py' -exec md5sum {} +) | sort -k2 | md5sum | cut -d' ' -f1)
    container_sum=$(docker exec "$CONTAINER" sh -c "cd /app/src && find . -name '*.py' -exec md5sum {} +" | sort -k2 | md5sum | cut -d' ' -f1)
    if [ "$host_sum" != "$container_sum" ]; then
        echo "error: host and container file lists match but contents differ - re-run without --no-sync." >&2
        return 1
    fi
    echo "    tree matches ($host_sum)"
}

[ "$SYNC" -eq 1 ] && sync_tree
if [ "$ALLOW_DRIFT" -eq 1 ]; then
    echo "==> skipping parity check (--allow-drift): the container is expected to differ"
else
    verify_parity
fi
[ "$VERIFY_ONLY" -eq 1 ] && exit 0

if [ "$FAST" -eq 1 ]; then
    # A stable name, so the database survives between runs and can be reused.
    DB_NAME="${UL_TEST_DB_NAME:-ul_fast}"
    if [ "$FRESH_DB" -eq 1 ]; then
        DB_FLAG="--create-db"
        echo "==> rebuilding the reusable database '$DB_NAME'"
    else
        DB_FLAG="--reuse-db"
        # --reuse-db does not apply new migrations to an existing database, so a
        # schema change shows up as a confusing column error rather than as a
        # missing migration. Rebuild with --fresh-db when models move.
        echo "==> reusing database '$DB_NAME' (run --fresh-db after any migration)"
    fi
else
    DB_NAME="${UL_TEST_DB_NAME:-t_$(date +%s)_$$}"
    DB_FLAG=""
fi

echo "==> pytest (UL_TEST_DB_NAME=$DB_NAME)"
if [ -n "$DB_FLAG" ]; then
    docker exec -e UL_TEST_DB_NAME="$DB_NAME" "$CONTAINER" /app/.venv/bin/python -m pytest "$DB_FLAG" "${args[@]}"
else
    docker exec -e UL_TEST_DB_NAME="$DB_NAME" "$CONTAINER" /app/.venv/bin/python -m pytest "${args[@]}"
fi
