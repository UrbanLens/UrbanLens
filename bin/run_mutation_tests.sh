#!/usr/bin/env bash
#
# Run mutation testing against the scoped modules configured in pyproject.toml.
#
# Mutation testing: if code changes and nothing fails, the test is vacuous.
#
# Scoped to money/privacy/wiki modules where a silent test costs most; widen `only_mutate` to cover more.
#
# Usage:
#   bin/run_mutation_tests.sh                 # run every configured mutant
#   bin/run_mutation_tests.sh --results       # list survivors from the last run
#   bin/run_mutation_tests.sh --show NAME     # show one mutant's diff
set -euo pipefail

CONTAINER="${UL_TEST_CONTAINER:-urbanlens_development_main_test_runner}"
# Reused DB: rebuilding costs minutes against seconds of tests.
DB_NAME="${UL_TEST_DB_NAME:-ul_fast}"
MAX_CHILDREN="${UL_MUTMUT_CHILDREN:-3}"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_TEST_CONTAINER or start the stack." >&2
    exit 2
fi

in_container() {
    docker exec -e UL_TEST_DB_NAME="$DB_NAME" "$CONTAINER" sh -c "cd /app && $1"
}

# mutmut is dev-only; install on demand.
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
        # Tree is recopied each run; a stale copy would mutate old code.
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
