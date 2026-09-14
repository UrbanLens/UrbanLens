#!/usr/bin/env bash
#
# Sample Postgres backend counts by role, once a second, into a CSV.
#
# Companion to the neighbour test: k6 shows whether the second user noticed, this shows why.
#
# Samples rather than snapshots: exhaustion spikes inside one phase and leaves nothing behind.
#
# Usage:
#   bin/perf/pg_activity_sampler.sh --container urbanlens_dev_db --out /tmp/pg.csv
#   bin/perf/pg_activity_sampler.sh --container ... --interval 2 &
#
# Stops on SIGINT/SIGTERM, so the caller can background it and kill it.

set -euo pipefail

CONTAINER=""
OUT=""
INTERVAL=1
DB_USER="${UL_DB_USER:-postgres}"
DB_NAME="${UL_DB_NAME:-postgres}"

usage() {
	cat <<-EOF
		Sample pg_stat_activity by role into a CSV.

		  --container NAME   Postgres container to exec psql in (required).
		  --out PATH         CSV to write (required).
		  --interval SEC     Seconds between samples (default: 1).
		  --user NAME        Postgres role to connect as (default: \$UL_DB_USER or postgres).
		  --db NAME          Database to connect to (default: \$UL_DB_NAME or postgres).

		Columns: iso_time, usename, application_name, state, backends, max_connections.
		One row per distinct (usename, application_name, state) per sample, so a
		sample with nothing connected writes no rows for that second - absence in
		the CSV means zero, not missing.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--container) CONTAINER="$2"; shift 2 ;;
		--out) OUT="$2"; shift 2 ;;
		--interval) INTERVAL="$2"; shift 2 ;;
		--user) DB_USER="$2"; shift 2 ;;
		--db) DB_NAME="$2"; shift 2 ;;
		-h | --help) usage; exit 0 ;;
		*) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done

if [[ -z "${CONTAINER}" || -z "${OUT}" ]]; then
	echo "error: --container and --out are both required." >&2
	usage >&2
	exit 2
fi

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
	echo "error: no container named ${CONTAINER}." >&2
	exit 1
fi

# Grouped in SQL: one round trip per sample, grouped by tier.
read -r -d '' QUERY <<-'SQL' || true
	SELECT
	    to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
	    coalesce(usename, '<none>'),
	    coalesce(nullif(application_name, ''), '<none>'),
	    coalesce(state, '<none>'),
	    count(*),
	    current_setting('max_connections')
	FROM pg_stat_activity
	WHERE datname IS NOT NULL
	GROUP BY 1, 2, 3, 4, 6
	ORDER BY 5 DESC
SQL

echo "iso_time,usename,application_name,state,backends,max_connections" >"${OUT}"
echo "sampling ${CONTAINER} every ${INTERVAL}s into ${OUT}; Ctrl-C to stop" >&2

running=1
trap 'running=0' INT TERM

while [[ ${running} -eq 1 ]]; do
	# `|| true` so one failed sample ends the run with a CSV gap, not a dead sampler.
	docker exec "${CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -At -F',' -c "${QUERY}" >>"${OUT}" 2>/dev/null || true
	sleep "${INTERVAL}"
done

echo "sampler stopped; $(($(wc -l <"${OUT}") - 1)) rows in ${OUT}" >&2
