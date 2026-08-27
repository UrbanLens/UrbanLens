#!/usr/bin/env bash
#
# Run mutation testing against the scoped modules configured in pyproject.toml.
#
# Mutation testing answers the question manual break-verification answers, but
# systematically: *if I change this code, does anything fail?* Four vacuous tests
# were caught by hand during the 2026-08-17 audit - a guard whose prefix matched
# too much, a fixture that never reached the code under test, one that serialised
# by accident, and a scaling measurement whose axis never grew. Doing that by
# hand only covers code you are actively editing.
#
# It found a real gap the first time it ran. `_lock_and_refresh` in the billing
# ledger had its `select_for_update()` replaced with `None` - keeping the refresh,
# dropping the lock - and every test still passed, because the tests used two
# in-process snapshots and a lock is invisible on one connection. The lock was
# untested; test_billing_ledger_lock.py now covers it with real threads.
#
# Deliberately scoped. A mutant costs a test run, so this targets modules where a
# silent test is most expensive - money, privacy, and the community-editable
# wiki - rather than the whole tree. Widen `only_mutate` in pyproject.toml to
# cover more, and expect roughly one mutant per second.
#
# Usage:
#   bin/run_mutation_tests.sh                 # run every configured mutant
#   bin/run_mutation_tests.sh --results       # list survivors from the last run
#   bin/run_mutation_tests.sh --show NAME     # show one mutant's diff
set -euo pipefail

CONTAINER="${UL_TEST_CONTAINER:-urbanlens_development_main_test_runner}"
# Reused rather than unique: mutation testing runs the same tests hundreds of
# times, and building a database costs ~3 minutes against ~3 seconds of tests.
DB_NAME="${UL_TEST_DB_NAME:-ul_fast}"
MAX_CHILDREN="${UL_MUTMUT_CHILDREN:-3}"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_TEST_CONTAINER or start the stack." >&2
    exit 2
fi

in_container() {
    docker exec -e UL_TEST_DB_NAME="$DB_NAME" "$CONTAINER" sh -c "cd /app && $1"
}

# mutmut is a dev-only tool and is not in the runtime image; install on demand.
if ! docker exec "$CONTAINER" test -x /app/.venv/bin/mutmut; then
    echo "==> installing mutmut into the container venv"
    docker exec "$CONTAINER" sh -c "cd /app && VIRTUAL_ENV=/app/.venv /app/.venv/bin/uv pip install mutmut" >/dev/null
fi

case "${1:-run}" in
    --results)
        in_container "/app/.venv/bin/mutmut results" | tr '\r' '\n' | grep -v "Generating mutants" | grep -v '^\s*$'
        ;;
    --show)
        [ $# -ge 2 ] || { echo "usage: $0 --show MUTANT_NAME" >&2; exit 2; }
        in_container "/app/.venv/bin/mutmut show $2" | tr '\r' '\n' | grep -v "Generating mutants"
        ;;
    *)
        echo "==> syncing working tree and config"
        docker cp src/urbanlens/. "$CONTAINER":/app/src/urbanlens/
        docker cp pyproject.toml "$CONTAINER":/app/pyproject.toml
        docker exec -u root "$CONTAINER" chown -R appuser:appuser /app/src/urbanlens

        echo "==> ensuring the reusable database exists ($DB_NAME)"
        in_container "/app/.venv/bin/python -m pytest src/urbanlens/core/tests/test_version.py -q --reuse-db" >/dev/null

        echo "==> mutating (this takes roughly one second per mutant)"
        # The copied tree is rebuilt each run; a stale one silently mutates old code.
        in_container "rm -rf mutants && /app/.venv/bin/mutmut run --max-children $MAX_CHILDREN" 2>&1 \
            | tr '\r' '\n' | grep -v "Generating mutants" | tail -5
        echo
        echo "Survivors (🙁) are changes no test noticed. Inspect with:"
        echo "  bin/run_mutation_tests.sh --results"
        echo "  bin/run_mutation_tests.sh --show <name>"
        echo
        echo "A survivor is not automatically a missing test - some sit in code the"
        echo "configured test selection does not cover at all, which the report marks"
        echo "separately as 'no tests'. The ones that matter are survivors in code you"
        echo "believed was covered."
        ;;
esac
