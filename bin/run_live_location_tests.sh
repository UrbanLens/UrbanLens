#!/usr/bin/env bash
#
# Run the live-locations suite (tests/live_locations): real REData answers for the campuses in
# tests/live_locations/kirkbrides.toml.
#
# By default only REData's own answers are checked, which needs nothing but a key. --pipeline also runs
# UrbanLens's own bootstrap against that REData (test_pipeline.py), which needs a test database: it goes
# through bin/host_pytest.sh, so the dev stack's test-runner and test-db must be up.
#
# Usage:
#   bin/run_live_location_tests.sh                       # the primary sites
#   bin/run_live_location_tests.sh --pipeline            # and the pin -> property pipeline for each
#   bin/run_live_location_tests.sh --sites all           # every catalogued campus
#   bin/run_live_location_tests.sh --sites hrsh,athens   # named sites
#   bin/run_live_location_tests.sh --report out.json     # write per-check results and every call
#   bin/run_live_location_tests.sh -- -k footprints -x   # pass through to pytest
#
# Environment (read from .env when unset):
#   UL_LIVE_REDATA_API_URL   REData origin, e.g. https://redata.example.org (no /api/v1)
#   UL_LIVE_REDATA_API_KEY   a key for that REData; a dedicated one, since a run spends its budget
#   UL_LIVE_REDATA_HOST      Host header, when the URL is an address rather than the site's name
#   UL_LIVE_MAX_WAIT_SECONDS how long to wait out a budget refusal before calling it inconclusive
#   UL_TEST_DB_NAME          the test database --pipeline creates (default test_live_locations)
#
# Runs serially: every site shares one REData key's hourly budget.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SITES="primary"
REPORT=""
PIPELINE=0
passthrough=()

while [ $# -gt 0 ]; do
    case "$1" in
        --sites) SITES="${2:-}"; shift 2 ;;
        --sites=*) SITES="${1#*=}"; shift ;;
        --report) REPORT="${2:-}"; shift 2 ;;
        --report=*) REPORT="${1#*=}"; shift ;;
        --) shift; passthrough+=("$@"); break ;;
        --pipeline) PIPELINE=1; shift ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) passthrough+=("$1"); shift ;;
    esac
done

# A worktree has no .env of its own; the main checkout's holds the key.
env_file=.env
[ -f "$env_file" ] || env_file="$(git rev-parse --path-format=absolute --git-common-dir)/../.env"
for name in UL_LIVE_REDATA_API_URL UL_LIVE_REDATA_API_KEY UL_LIVE_REDATA_HOST UL_LIVE_MAX_WAIT_SECONDS; do
    if [ -z "${!name:-}" ] && [ -f "$env_file" ]; then
        value=$(sed -n "s/^${name}=//p" "$env_file" | tr -d "\"'" | head -1)
        [ -n "$value" ] && export "$name=$value"
    fi
done

if [ -z "${UL_LIVE_REDATA_API_URL:-}" ] || [ -z "${UL_LIVE_REDATA_API_KEY:-}" ]; then
    echo "error: set UL_LIVE_REDATA_API_URL and UL_LIVE_REDATA_API_KEY (environment or .env)." >&2
    exit 2
fi

args=(tests/live_locations -p no:cacheprovider -rfEs --live-sites "$SITES")
[ -n "$REPORT" ] && args+=(--live-report "$REPORT")
export UL_LIVE_LOCATIONS=1

if [ "$PIPELINE" = 1 ]; then
    export UL_TEST_DB_NAME="${UL_TEST_DB_NAME:-test_live_locations}"
    exec bash bin/host_pytest.sh "${args[@]}" "${passthrough[@]}"
fi
# Without --pipeline there is no test database, so neither test that needs one can run here. test_open_buildings.py
# asks Google's bucket directly rather than REData and has its own command (docs/LOCATION_DATA_TESTS.md).
exec uv run --frozen pytest "${args[@]}" --ignore=tests/live_locations/test_pipeline.py \
    --ignore=tests/live_locations/test_open_buildings.py "${passthrough[@]}"
