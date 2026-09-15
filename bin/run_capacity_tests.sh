#!/usr/bin/env bash
#
# Measure how many signed-in people one deployment serves at once, and return a verdict.
#
# Provisions a population, climbs through levels of concurrent users, and samples
# Postgres, every container's CPU and memory, and the proxy's own log throughout.
#
# Usage:
#   bin/run_capacity_tests.sh --url http://localhost:31000 \
#       --provision-container ul_perf_app --db-container ul_perf_db \
#       --container-prefix ul_perf_ --nginx-container ul_perf_nginx --population 1000
#
# Anything after `--` is handed to `k6 run` unchanged.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE_DIR="${REPO_ROOT}/tests/perf"

BASE_URL="${UL_PERF_BASE_URL:-}"
MANIFEST=""
PROVISION_CONTAINER=""
POPULATION=1000
DB_CONTAINER=""
NGINX_CONTAINER=""
CONTAINER_PREFIX=""
LEVELS=""
HOLD_SECONDS=""
THINK_MEDIAN=""
SOCKETS=1
OUT_DIR=""
K6_IMAGE="${UL_PERF_K6_IMAGE:-grafana/k6:latest}"
PASSTHROUGH=()

usage() {
	cat <<-EOF
		Measure concurrent-user capacity and return a verdict.

		  --url URL                  The deployment to measure (required).
		  --manifest PATH            A population manifest already written by
		                             provision_integration_env --population.
		  --provision-container NAME Provision the population by exec'ing the
		                             management command here, then copy the
		                             manifest out. Required unless --manifest.
		  --population N             Accounts to provision (default: ${POPULATION}).
		  --db-container NAME        Sample pg_stat_activity here.
		  --container-prefix TEXT    Sample CPU and memory of every container
		                             whose name starts with this.
		  --nginx-container NAME     Keep this proxy's access log for the run.
		  --levels A,B,C             Concurrent users at each hold (default: 100,250,500,1000).
		  --hold-seconds N           Seconds per hold (default: 240).
		  --think-median N           Median seconds on a page (default: 30).
		  --no-sockets               Leave out the notification socket.
		  --out DIR                  Where results go (default: a timestamped dir
		                             under tests/perf/results).
		  -- ARGS...                 Everything after this goes to \`k6 run\`.

		This refuses to run against anything that looks like production or
		staging. It creates accounts and writes rows as real users.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--url) BASE_URL="$2"; shift 2 ;;
		--manifest) MANIFEST="$2"; shift 2 ;;
		--provision-container) PROVISION_CONTAINER="$2"; shift 2 ;;
		--population) POPULATION="$2"; shift 2 ;;
		--db-container) DB_CONTAINER="$2"; shift 2 ;;
		--nginx-container) NGINX_CONTAINER="$2"; shift 2 ;;
		--container-prefix) CONTAINER_PREFIX="$2"; shift 2 ;;
		--levels) LEVELS="$2"; shift 2 ;;
		--hold-seconds) HOLD_SECONDS="$2"; shift 2 ;;
		--think-median) THINK_MEDIAN="$2"; shift 2 ;;
		--no-sockets) SOCKETS=0; shift ;;
		--out) OUT_DIR="$2"; shift 2 ;;
		-h | --help) usage; exit 0 ;;
		--) shift; PASSTHROUGH=("$@"); break ;;
		*) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done

refuse() {
	echo "error: $1" >&2
	echo "This suite creates accounts and writes rows as real users. It is for dev environments only." >&2
	exit 2
}

if [[ -z "${BASE_URL}" ]]; then
	echo "error: no target. Pass --url." >&2
	usage >&2
	exit 2
fi

case "${BASE_URL}" in
	*staging*) refuse "${BASE_URL} names staging." ;;
	*prod*) refuse "${BASE_URL} names production." ;;
	*.dev.urbanlens.org*) ;;
	*urbanlens.org*) refuse "${BASE_URL} is on urbanlens.org but is not a *.dev.urbanlens.org environment." ;;
esac

for container in "${PROVISION_CONTAINER}" "${DB_CONTAINER}" "${NGINX_CONTAINER}" "${CONTAINER_PREFIX}"; do
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
	OUT_DIR="${SUITE_DIR}/results/capacity-$(date -u +%Y%m%dT%H%M%SZ)"
fi
mkdir -p "${OUT_DIR}"
OUT_DIR="$(cd "${OUT_DIR}" && pwd)"
echo "==> results in ${OUT_DIR}"

# -- population --------------------------------------------------------------

if [[ -n "${PROVISION_CONTAINER}" ]]; then
	echo "==> provisioning ${POPULATION} accounts in ${PROVISION_CONTAINER}"
	REMOTE_MANIFEST="/tmp/ul-capacity-manifest.json"
	docker exec "${PROVISION_CONTAINER}" /app/.venv/bin/python src/urbanlens/manage.py provision_integration_env \
		--population "${POPULATION}" --out "${REMOTE_MANIFEST}"
	MANIFEST="${OUT_DIR}/manifest.json"
	docker cp "${PROVISION_CONTAINER}:${REMOTE_MANIFEST}" "${MANIFEST}"
	# Live sessions for every account; one copy, owner-only.
	chmod 600 "${MANIFEST}"
	docker exec "${PROVISION_CONTAINER}" rm -f "${REMOTE_MANIFEST}"
fi

if [[ ! -f "${MANIFEST}" ]]; then
	echo "error: no manifest at ${MANIFEST}." >&2
	exit 1
fi
MANIFEST="$(cd "$(dirname "${MANIFEST}")" && pwd)/$(basename "${MANIFEST}")"

# -- samplers ----------------------------------------------------------------

SAMPLER_PIDS=()
stop_samplers() {
	for pid in ${SAMPLER_PIDS[@]+"${SAMPLER_PIDS[@]}"}; do
		kill "${pid}" 2>/dev/null || true
		wait "${pid}" 2>/dev/null || true
	done
	SAMPLER_PIDS=()
}
trap stop_samplers EXIT

if [[ -n "${DB_CONTAINER}" ]]; then
	bash "${REPO_ROOT}/bin/perf/pg_activity_sampler.sh" --container "${DB_CONTAINER}" --out "${OUT_DIR}/pg_activity.csv" &
	SAMPLER_PIDS+=($!)
fi
if [[ -n "${CONTAINER_PREFIX}" ]]; then
	bash "${REPO_ROOT}/bin/perf/container_sampler.sh" --prefix "${CONTAINER_PREFIX}" --out "${OUT_DIR}/containers.csv" &
	SAMPLER_PIDS+=($!)
fi

# -- k6 ----------------------------------------------------------------------

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "==> capacity run"
STATUS=0
# --ulimit because every VU holds a socket or two; the image's default cap is below a thousand users.
docker run --rm -i \
	--network=host \
	--ulimit nofile=65536:65536 \
	--user "$(id -u):$(id -g)" \
	-v "${SUITE_DIR}:/perf:ro" \
	-v "${MANIFEST}:/manifest.json:ro" \
	-v "${OUT_DIR}:/out" \
	-e UL_PERF_BASE_URL="${BASE_URL}" \
	-e UL_PERF_MANIFEST=/manifest.json \
	-e UL_PERF_SUMMARY=/out/capacity.json \
	-e UL_CAP_STAGES_PATH=/out/stages.json \
	-e UL_CAP_SOCKETS="${SOCKETS}" \
	${LEVELS:+-e UL_CAP_LEVELS="${LEVELS}"} \
	${HOLD_SECONDS:+-e UL_CAP_HOLD_SECONDS="${HOLD_SECONDS}"} \
	${THINK_MEDIAN:+-e UL_CAP_THINK_MEDIAN_SECONDS="${THINK_MEDIAN}"} \
	"${K6_IMAGE}" run ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} /perf/k6/population.js || STATUS=$?

stop_samplers
trap - EXIT

if [[ -n "${NGINX_CONTAINER}" ]]; then
	docker logs --since "${STARTED}" "${NGINX_CONTAINER}" >"${OUT_DIR}/nginx.log" 2>&1 || true
fi

POOL_STATUS=0
if [[ -n "${DB_CONTAINER}" ]]; then
	docker logs --since "${STARTED}" "${DB_CONTAINER}" >"${OUT_DIR}/db.log" 2>&1 || true
	python3 "${REPO_ROOT}/bin/perf/report_activity.py" "${OUT_DIR}/pg_activity.csv" \
		--fail-on-pressure --db-log "${OUT_DIR}/db.log" || POOL_STATUS=$?
fi

REPORT_ARGS=(--summary "${OUT_DIR}/capacity.json" --stages "${OUT_DIR}/stages.json" --out "${OUT_DIR}/report.md")
[[ -f "${OUT_DIR}/containers.csv" ]] && REPORT_ARGS+=(--containers "${OUT_DIR}/containers.csv")
[[ -f "${OUT_DIR}/nginx.log" ]] && REPORT_ARGS+=(--nginx-log "${OUT_DIR}/nginx.log")
if [[ -f "${OUT_DIR}/capacity.json" && -f "${OUT_DIR}/stages.json" ]]; then
	python3 "${REPO_ROOT}/bin/perf/report_capacity.py" "${REPORT_ARGS[@]}" || true
fi

echo
if [[ ${STATUS} -eq 0 && ${POOL_STATUS} -eq 0 ]]; then
	echo "PASS - every hold stayed inside its budgets, and the connection pool held."
elif [[ ${STATUS} -ne 0 ]]; then
	echo "FAIL - see the per-hold table above and ${OUT_DIR}/report.md. k6 exited ${STATUS}."
else
	echo "FAIL - latency held but the connection pool did not. See the pg_stat_activity lines above."
	STATUS="${POOL_STATUS}"
fi
echo "Results: ${OUT_DIR}"
exit "${STATUS}"
