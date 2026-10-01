#!/usr/bin/env bash
#
# Sample CPU, throttling and memory for every container of one stack into a CSV.
#
# Reads cgroup v2 counters straight from the host, so a sample costs no exec and
# does not itself load the containers it measures. CPU is recorded as cumulative
# counters; the report turns consecutive samples into cores and throttled share.
#
# Usage:
#   bin/perf/container_sampler.sh --prefix ul_perf_ --out /tmp/containers.csv &
#
# Stops on SIGINT/SIGTERM, so the caller can background it and kill it.

set -euo pipefail

PREFIX=""
OUT=""
INTERVAL=2
CGROUP_ROOT="${UL_PERF_CGROUP_ROOT:-/sys/fs/cgroup/system.slice}"

usage() {
	cat <<-EOF
		Sample cgroup v2 CPU and memory for a stack's containers into a CSV.

		  --prefix TEXT      Container name prefix, e.g. ul_perf_ (required).
		  --out PATH         CSV to write (required).
		  --interval SEC     Seconds between samples (default: ${INTERVAL}).

		Columns: epoch_ms, container, usage_usec, throttled_usec, nr_throttled,
		memory_bytes, cpu_max. usage_usec and throttled_usec are cumulative.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--out) OUT="$2"; shift 2 ;;
		--interval) INTERVAL="$2"; shift 2 ;;
		-h | --help) usage; exit 0 ;;
		*) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
	esac
done

if [[ -z "${PREFIX}" || -z "${OUT}" ]]; then
	echo "error: --prefix and --out are both required." >&2
	usage >&2
	exit 2
fi

echo "epoch_ms,container,usage_usec,throttled_usec,nr_throttled,memory_bytes,cpu_max" >"${OUT}"
echo "sampling containers named ${PREFIX}* every ${INTERVAL}s into ${OUT}; Ctrl-C to stop" >&2

running=1
trap 'running=0' INT TERM

# Re-listed every sample: a container recreated mid-run gets a new id.
while [[ ${running} -eq 1 ]]; do
	now_ms="$(date +%s%3N)"
	while read -r id name; do
		scope="${CGROUP_ROOT}/docker-${id}.scope"
		[[ -r "${scope}/cpu.stat" ]] || continue
		awk -v now="${now_ms}" -v name="${name}" \
			-v memory="$(cat "${scope}/memory.current" 2>/dev/null || echo 0)" \
			-v cpumax="$(tr ' ' '/' <"${scope}/cpu.max" 2>/dev/null || echo max)" \
			'$1 == "usage_usec" { usage = $2 } $1 == "throttled_usec" { throttled = $2 } $1 == "nr_throttled" { periods = $2 }
			END { printf "%s,%s,%s,%s,%s,%s,%s\n", now, name, usage, throttled + 0, periods + 0, memory, cpumax }' \
			"${scope}/cpu.stat" >>"${OUT}" || true
	done < <(docker ps --no-trunc --filter "name=^${PREFIX}" --format '{{.ID}} {{.Names}}' 2>/dev/null || true)
	sleep "${INTERVAL}"
done

echo "sampler stopped; $(($(wc -l <"${OUT}") - 1)) rows in ${OUT}" >&2
