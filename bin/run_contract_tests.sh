#!/usr/bin/env bash
#
# Run the OpenAPI conformance suite (tests/contract).
#
# Two modes, and they answer different questions:
#
#   in-process (default) - generate the schema from the urlconf and drive
#       Django's WSGI callable directly. Needs a database, needs no deployment.
#       This is the mode to run before merging a serializer change.
#
#   live (--url) - fetch the schema from a deployment and call it over HTTP.
#       Needs no database and no container. This is the mode that answers
#       "does what we shipped still match its published contract".
#
# Usage:
#   bin/run_contract_tests.sh                        # in-process, safe methods
#   bin/run_contract_tests.sh --methods all          # include writes
#   bin/run_contract_tests.sh --examples 40          # search harder
#   bin/run_contract_tests.sh --url https://s1.dev.urbanlens.org
#   bin/run_contract_tests.sh --local                # host venv, not the container
#   bin/run_contract_tests.sh -- -k pins -x          # pass through to pytest
#
# --methods all makes the run create, modify and delete data as the account it
# holds a key for. That is fine against a test database or a provisioned
# throwaway account, and is why `safe` is the default.
#
# Environment:
#   UL_TEST_CONTAINER   test-runner container (default urbanlens_development_main_test_runner)
#   UL_TEST_DB_NAME     test database name; required in-process, so concurrent
#                       runs do not collide. Generated when unset.
#   UL_CONTRACT_API_KEY live mode only: the key to authenticate with. Falls back
#                       to UL_E2E_ACCOUNTS_FILE, the manifest
#                       `provision_integration_env --out` writes.
set -euo pipefail

CONTAINER="${UL_TEST_CONTAINER:-urbanlens_development_main_test_runner}"
URL=""
METHODS="safe"
EXAMPLES=""
LOCAL=0
passthrough=()

while [ $# -gt 0 ]; do
    case "$1" in
        --url) URL="${2:-}"; shift 2 ;;
        --url=*) URL="${1#*=}"; shift ;;
        --methods) METHODS="${2:-}"; shift 2 ;;
        --methods=*) METHODS="${1#*=}"; shift ;;
        --examples) EXAMPLES="${2:-}"; shift 2 ;;
        --examples=*) EXAMPLES="${1#*=}"; shift ;;
        --local) LOCAL=1; shift ;;
        --) shift; passthrough+=("$@"); break ;;
        -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) passthrough+=("$1"); shift ;;
    esac
done

if [ "$METHODS" != "safe" ] && [ "$METHODS" != "all" ]; then
    echo "error: --methods must be 'safe' or 'all', not '$METHODS'." >&2
    exit 2
fi

env_args=("UL_CONTRACT_METHODS=$METHODS")
[ -n "$EXAMPLES" ] && env_args+=("UL_CONTRACT_MAX_EXAMPLES=$EXAMPLES")

if [ -n "$URL" ]; then
    case "$URL" in
        https://urbanlens.org|https://www.urbanlens.org|https://app.urbanlens.org)
            echo "error: refusing to fuzz production ($URL)." >&2
            exit 2
            ;;
    esac
    env_args+=("UL_CONTRACT_BASE_URL=$URL")
    # Live mode reaches a remote host on purpose. tests/contract does not load
    # src/urbanlens/conftest.py - the localhost-only network guard lives there
    # and is not in effect - but say so explicitly so the run does not depend on
    # that staying true.
    env_args+=("UL_ALLOW_TEST_INTERNET=True")
    [ -n "${UL_CONTRACT_API_KEY:-}" ] && env_args+=("UL_CONTRACT_API_KEY=$UL_CONTRACT_API_KEY")
    [ -n "${UL_E2E_ACCOUNTS_FILE:-}" ] && env_args+=("UL_E2E_ACCOUNTS_FILE=$UL_E2E_ACCOUNTS_FILE")
    echo "==> live mode against $URL (methods: $METHODS)"
else
    # A unique name by default, for the same reason bin/run_tests.sh does it:
    # two runs sharing a test database corrupt each other's fixtures.
    env_args+=("UL_TEST_DB_NAME=${UL_TEST_DB_NAME:-ctr_$(date +%s)_$$}")
    # Several endpoints enqueue work; without eager mode they fail against a
    # broker that is not running rather than exercising the endpoint.
    env_args+=("UL_CELERY_TASK_ALWAYS_EAGER=True")
    echo "==> in-process mode (methods: $METHODS)"
fi

if [ "$LOCAL" -eq 1 ] || [ -n "$URL" ]; then
    # Live mode needs neither the container nor a database, so it runs wherever
    # it was invoked - which is also what makes it usable from CI.
    exec env "${env_args[@]}" python -m pytest tests/contract "${passthrough[@]}"
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_TEST_CONTAINER, start the stack, or use --local." >&2
    exit 2
fi

# bin/run_tests.sh syncs src/ only; this suite lives outside it, so its own copy
# has to happen here. Both are copied because the tests import the application.
echo "==> syncing into $CONTAINER"
docker cp src/. "$CONTAINER":/app/src/
docker exec -u root "$CONTAINER" mkdir -p /app/tests
docker cp tests/contract "$CONTAINER":/app/tests/
docker cp pyproject.toml "$CONTAINER":/app/pyproject.toml
docker exec -u root "$CONTAINER" chown -R appuser:appuser /app/src /app/tests /app/pyproject.toml

docker_env=()
for pair in "${env_args[@]}"; do docker_env+=(-e "$pair"); done
exec docker exec "${docker_env[@]}" "$CONTAINER" /app/.venv/bin/python -m pytest tests/contract "${passthrough[@]}"
