#!/usr/bin/env bash
#
# Run the neighbour load test against a deployment, and return a verdict.
#
# Verdict is about one account's latency while a different account does progressively costlier things.
#
# Seeds the target, baselines on the same host first, and samples pg_stat_activity throughout.
#
# Usage:
#   bin/run_perf_tests.sh --url http://localhost:21810 \
#       --provision-container urbanlens_development_main_app \
#       --db-container urbanlens_development_main_db --heavy-pins 20000
#
#   bin/run_perf_tests.sh --url https://perf.dev.urbanlens.org --manifest /tmp/perf.json
#
# Anything after `--` is handed to `k6 run` unchanged.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE_DIR="${REPO_ROOT}/tests/perf"

BASE_URL="${UL_PERF_BASE_URL:-}"
MANIFEST=""
PROVISION_CONTAINER=""
DB_CONTAINER=""
HEAVY_PINS=20000
RATE=5
BASELINE_SECONDS=60
BUDGET_MS=""
OUT_DIR=""
K6_IMAGE="${UL_PERF_K6_IMAGE:-grafana/k6:latest}"
SKIP_BASELINE=0
PHASES=""
PASSTHROUGH=()

usage() {
	cat <<-EOF
		Run the neighbour load test and return a verdict.

		  --url URL                  The deployment to measure (required).
		  --manifest PATH            An account manifest already written by
		                             provision_integration_env. Required unless
		                             --provision-container is given.
		  --provision-container NAME Provision and seed by exec'ing the management
		                             command in this container, then read the
		                             manifest back out of it.
		  --db-container NAME        Sample pg_stat_activity here during the run.
		  --heavy-pins N             Pins to seed the heavy account to (default: ${HEAVY_PINS}).
		  --rate N                   Neighbour requests per second (default: ${RATE}).
		  --budget-ms N              Skip the baseline pass and use this p95 ceiling.
		  --baseline-seconds N       Baseline pass length (default: ${BASELINE_SECONDS}).
		  --phases A,B               Run only these phases (the baseline is always
		                             kept). A full run is ~15 minutes; this is how
		                             you measure one phase, or pick up the one a
		                             cut-short run never reached.
		  --out DIR                  Where summaries and the sampler CSV go
		                             (default: a timestamped dir under tests/perf/results).
		  -- ARGS...                 Everything after this goes to \`k6 run\`.

		This refuses to run against anything that looks like production or
		staging. It writes rows, edits labels and imports pins as a real user.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--url) BASE_URL="$2"; shift 2 ;;
		--manifest) MANIFEST="$2"; shift 2 ;;
		--provision-container) PROVISION_CONTAINER="$2"; shift 2 ;;
		--db-container) DB_CONTAINER="$2"; shift 2 ;;
		--heavy-pins) HEAVY_PINS="$2"; shift 2 ;;
		--rate) RATE="$2"; shift 2 ;;
		--budget-ms) BUDGET_MS="$2"; SKIP_BASELINE=1; shift 2 ;;
		--baseline-seconds) BASELINE_SECONDS="$2"; shift 2 ;;
		--phases) PHASES="$2"; shift 2 ;;
		--out) OUT_DIR="$2"; shift 2 ;;
		-h | --help) usage; exit 0 ;;
		--) shift; PASSTHROUGH=("$@"); break ;;
		*) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done

# -- refuse anywhere that matters -------------------------------------------
#
# URL and container name are checked independently; either one refuses.
refuse() {
	echo "error: $1" >&2
	echo "This suite writes rows, edits labels and imports pins as a real user. It is for dev environments only." >&2
	exit 2
}

if [[ -z "${BASE_URL}" ]]; then
	echo "error: no target. Pass --url." >&2
	usage >&2
	exit 2
fi

# Allow-list: only *.dev.urbanlens.org is definitely safe.
case "${BASE_URL}" in
	*staging*) refuse "${BASE_URL} names staging." ;;
	*prod*) refuse "${BASE_URL} names production." ;;
	*.dev.urbanlens.org*) ;;
	*urbanlens.org*) refuse "${BASE_URL} is on urbanlens.org but is not a *.dev.urbanlens.org environment." ;;
esac

for container in "${PROVISION_CONTAINER}" "${DB_CONTAINER}"; do
	case "${container}" in
		urbanlens_production_*) refuse "${container} is a production container." ;;
		urbanlens_staging_*) refuse "${container} is a staging container." ;;
	esac
done

if [[ -z "${MANIFEST}" && -z "${PROVISION_CONTAINER}" ]]; then
	echo "error: pass --manifest, or --provision-container so one can be made." >&2
	exit 2
fi

command -v docker >/dev/null 2>&1 || { echo "error: docker is not on PATH; k6 runs in a container." >&2; exit 1; }

if [[ -z "${OUT_DIR}" ]]; then
	OUT_DIR="${SUITE_DIR}/results/$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "${OUT_DIR}"
echo "==> results in ${OUT_DIR}"

# -- seed --------------------------------------------------------------------

if [[ -n "${PROVISION_CONTAINER}" ]]; then
	echo "==> provisioning and seeding in ${PROVISION_CONTAINER} (${HEAVY_PINS} pins)"
	REMOTE_MANIFEST="/tmp/ul-perf-manifest.json"
	docker exec "${PROVISION_CONTAINER}" /app/.venv/bin/python src/urbanlens/manage.py provision_integration_env \
		--roles primary,secondary,heavy \
		--heavy-pins "${HEAVY_PINS}" \
		--out "${REMOTE_MANIFEST}"
	MANIFEST="${OUT_DIR}/manifest.json"
	docker cp "${PROVISION_CONTAINER}:${REMOTE_MANIFEST}" "${MANIFEST}"
	# Manifest holds live credentials on a shared host.
	chmod 600 "${MANIFEST}"
fi

if [[ ! -f "${MANIFEST}" ]]; then
	echo "error: no manifest at ${MANIFEST}." >&2
	exit 1
fi
MANIFEST="$(cd "$(dirname "${MANIFEST}")" && pwd)/$(basename "${MANIFEST}")"

# -- k6 ----------------------------------------------------------------------

run_k6() {
	local summary_name="$1"
	shift
	# --network=host so local targets resolve; manifest mounted read-only at a fixed path.
	# --user so the mode-600 manifest stays readable without loosening it on a shared host.
	docker run --rm -i \
		--network=host \
		--user "$(id -u):$(id -g)" \
		-v "${SUITE_DIR}:/perf:ro" \
		-v "${MANIFEST}:/manifest.json:ro" \
		-v "${OUT_DIR}:/out" \
		-e UL_PERF_BASE_URL="${BASE_URL}" \
		-e UL_PERF_MANIFEST=/manifest.json \
		-e UL_PERF_SUMMARY="/out/${summary_name}" \
		-e UL_PERF_RATE="${RATE}" \
		-e UL_PERF_BASELINE_SECONDS="${BASELINE_SECONDS}" \
		${PHASES:+-e UL_PERF_PHASES="${PHASES}"} \
		"$@" \
		"${K6_IMAGE}" run ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} /perf/k6/neighbour.js
}

if [[ ${SKIP_BASELINE} -eq 0 ]]; then
	echo "==> baseline pass (${BASELINE_SECONDS}s, neighbour only)"
	run_k6 "baseline.json" -e UL_PERF_BASELINE=1
	BUDGET_MS="$(python3 "${REPO_ROOT}/bin/perf/derive_budget.py" "${OUT_DIR}/baseline.json")"
	echo "==> budget derived from this host, this run: p95 < ${BUDGET_MS}ms"
fi

SAMPLER_PID=""
if [[ -n "${DB_CONTAINER}" ]]; then
	bash "${REPO_ROOT}/bin/perf/pg_activity_sampler.sh" --container "${DB_CONTAINER}" --out "${OUT_DIR}/pg_activity.csv" &
	SAMPLER_PID=$!
	# Killed on any exit, not just the happy one: an orphaned sampler goes on
	# exec'ing psql once a second forever.
	trap 'kill "${SAMPLER_PID}" 2>/dev/null || true' EXIT
fi

echo "==> measured pass"
STATUS=0
run_k6 "measured.json" \
	-e UL_PERF_BUDGET_MS="${BUDGET_MS}" \
	-e UL_PERF_EXPECTED_PINS="${HEAVY_PINS}" || STATUS=$?

POOL_STATUS=0
if [[ -n "${SAMPLER_PID}" ]]; then
	kill "${SAMPLER_PID}" 2>/dev/null || true
	wait "${SAMPLER_PID}" 2>/dev/null || true
	trap - EXIT
	# The pool is the other half of the verdict. Latency can look survivable
	# while one account holds every connection - that is what P104 was.
	docker logs "${DB_CONTAINER}" >"${OUT_DIR}/db.log" 2>&1 || true
	python3 "${REPO_ROOT}/bin/perf/report_activity.py" "${OUT_DIR}/pg_activity.csv" \
		--fail-on-pressure --db-log "${OUT_DIR}/db.log" || POOL_STATUS=$?
fi

echo
if [[ ${STATUS} -eq 0 && ${POOL_STATUS} -eq 0 ]]; then
	echo "PASS - the neighbour stayed inside ${BUDGET_MS}ms in every phase, and the connection pool held."
elif [[ ${STATUS} -ne 0 ]]; then
	echo "FAIL - see the per-phase table above. k6 exited ${STATUS}."
else
	echo "FAIL - latency held but the connection pool did not. See the pg_stat_activity lines above."
	STATUS="${POOL_STATUS}"
fi
echo "Summaries: ${OUT_DIR}"
exit "${STATUS}"
