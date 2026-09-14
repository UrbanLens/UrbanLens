#!/usr/bin/env bash
#
# Ask a running container's Python what it is doing right now.
#
# For CPU-burning, answerless processes where logs stop with no traceback: a sampler attached from outside.
#
# Runs py-spy in a sidecar sharing the target's PID namespace, so the image never carries SYS_PTRACE.
#
# Usage:
#   bin/perf/pyspy.sh urbanlens_development_main_app            # a live view
#   bin/perf/pyspy.sh urbanlens_development_main_app dump       # one snapshot
#   bin/perf/pyspy.sh urbanlens_development_main_app record 30 out.svg
#
# Attach fails at ptrace_scope 2+ regardless of capabilities.

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

# PID 1 is usually the entrypoint; profile the busiest Python instead.
TARGET_PID="$(docker exec "${CONTAINER}" sh -c "ps -eo pid,pcpu,comm --sort=-pcpu | awk '\$3 ~ /python/ {print \$1; exit}'" 2>/dev/null || true)"
if [[ -z "${TARGET_PID}" ]]; then
	echo "error: no python process in ${CONTAINER}." >&2
	exit 1
fi
echo "==> attaching to pid ${TARGET_PID} (in-container) of ${CONTAINER}" >&2

# Stock Python image + pip install: no official py-spy image to pin. Only SYS_PTRACE is granted, never --privileged.
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
