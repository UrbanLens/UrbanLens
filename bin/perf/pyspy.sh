#!/usr/bin/env bash
#
# Ask a running container's Python what it is doing right now.
#
# For the case where a process is burning CPU and answering nothing: logs stop,
# the healthcheck fails, and there is no traceback because nothing raised. A
# sampling profiler attached from outside is the only thing that can say where
# the time is going without the process having been started specially.
#
# Runs `py-spy` in a sidecar sharing the target's PID namespace rather than
# installing it into the image. The production service therefore never carries
# `SYS_PTRACE`; only this throwaway container does, and only while it runs.
#
# Usage:
#   bin/perf/pyspy.sh urbanlens_development_main_app            # a live view
#   bin/perf/pyspy.sh urbanlens_development_main_app dump       # one snapshot
#   bin/perf/pyspy.sh urbanlens_development_main_app record 30 out.svg
#
# Needs `kernel.yama.ptrace_scope` at 0 or 1 on the host. At 2 or 3 the kernel
# refuses cross-process attach whatever the capabilities say, and the failure
# reads as "permission denied" rather than as a kernel policy.

set -euo pipefail

CONTAINER="${1:-}"
MODE="${2:-top}"
DURATION="${3:-30}"
OUTPUT="${4:-}"

if [[ -z "${CONTAINER}" ]]; then
	echo "usage: $0 <container> [top|dump|record] [seconds] [output.svg]" >&2
	exit 2
fi

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
	echo "error: no container named ${CONTAINER}." >&2
	exit 1
fi

SCOPE="$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo 0)"
if [[ "${SCOPE}" -gt 1 ]]; then
	echo "error: kernel.yama.ptrace_scope is ${SCOPE}; attaching to another process is refused by the kernel regardless of capabilities." >&2
	echo "       sudo sysctl -w kernel.yama.ptrace_scope=1   (until reboot)" >&2
	exit 1
fi

# The Python worth looking at is rarely PID 1 - that is usually an entrypoint
# script. Pick the busiest Python instead, which is the one that is stuck.
TARGET_PID="$(docker exec "${CONTAINER}" sh -c "ps -eo pid,pcpu,comm --sort=-pcpu | awk '\$3 ~ /python/ {print \$1; exit}'" 2>/dev/null || true)"
if [[ -z "${TARGET_PID}" ]]; then
	echo "error: no python process in ${CONTAINER}." >&2
	exit 1
fi
echo "==> attaching to pid ${TARGET_PID} (in-container) of ${CONTAINER}" >&2

# A stock Python image with py-spy installed at run time, rather than a
# published py-spy image: there is no official one, and pinning some individual's
# is a supply-chain dependency for a debugging tool. `pip install` costs a few
# seconds and the sidecar is thrown away either way.
#
# --privileged is deliberately not used; SYS_PTRACE is the one capability
# needed, and only this throwaway container ever holds it.
SPY_IMAGE="${UL_PYSPY_IMAGE:-python:3.12-slim}"

run_spy() {
	local spy_args="$*"
	docker run --rm \
		--pid="container:${CONTAINER}" \
		--cap-add=SYS_PTRACE \
		${OUTPUT:+-v "$(cd "$(dirname "${OUTPUT}")" && pwd):/out"} \
		"${SPY_IMAGE}" \
		sh -c "pip install --quiet --disable-pip-version-check py-spy && py-spy ${spy_args}"
}

case "${MODE}" in
	top)
		exec docker run --rm -it \
			--pid="container:${CONTAINER}" \
			--cap-add=SYS_PTRACE \
			"${SPY_IMAGE}" \
			sh -c "pip install --quiet --disable-pip-version-check py-spy && py-spy top --pid ${TARGET_PID}"
		;;
	dump) run_spy "dump --pid ${TARGET_PID} --locals" ;;
	record)
		if [[ -z "${OUTPUT}" ]]; then
			echo "error: record needs an output path, e.g. $0 ${CONTAINER} record 30 /tmp/flame.svg" >&2
			exit 2
		fi
		run_spy "record --pid ${TARGET_PID} --duration ${DURATION} --output /out/$(basename "${OUTPUT}")"
		echo "wrote ${OUTPUT}" >&2
		;;
	*)
		echo "error: unknown mode ${MODE}; use top, dump or record." >&2
		exit 2
		;;
esac
