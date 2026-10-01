#!/usr/bin/env bash
#
# Run pytest on the host against the dev stack's test_db.
#
# test_db shares the test-runner's network namespace and publishes no port, so it is reached at the
# test-runner's bridge IP with the test-runner's credentials. psycopg connects below Python's socket
# layer, so the tests' localhost-only network guard does not see that connection. A test that opens
# its own Redis or Postgres socket still fails here; run it with bin/run_tests.sh.
#
# Usage:
#   UL_TEST_DB_NAME=test_<unique> bin/host_pytest.sh --reuse-db src/urbanlens/... -k ...
#
# UL_TEST_CONTAINER names a test-runner other than this checkout's.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -z "${UL_TEST_DB_NAME:-}" ]; then
    echo "error: set a unique UL_TEST_DB_NAME, so this run cannot drop another session's database." >&2
    exit 2
fi

name=$(sed -n 's/^UL_CONTAINER_NAME=//p' .env 2>/dev/null | tr -d "\"'" | head -1)
runner="${UL_TEST_CONTAINER:-urbanlens_${name}_test_runner}"
db="${runner%_test_runner}_test_db"

for container in "$runner" "$db"; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null)" != "true" ]; then
        echo "error: $container is not running. Start it: docker compose --profile test up -d test-runner test-db" >&2
        exit 2
    fi
done

runner_env() {
    docker inspect "$runner" --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n "s/^$1=//p" | head -1
}

UL_DB_HOST=$(docker inspect "$runner" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $1}')
UL_DB_PORT=$(runner_env UL_DB_PORT)
UL_DB_USER=$(runner_env UL_DB_USER)
UL_DB_PASS=$(runner_env UL_DB_PASS)
UL_DB_NAME=$(runner_env UL_DB_NAME)
export UL_DB_HOST UL_DB_PORT UL_DB_USER UL_DB_PASS UL_DB_NAME

exec uv run pytest "$@"
