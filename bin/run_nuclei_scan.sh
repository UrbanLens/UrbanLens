#!/usr/bin/env bash
#
# Run a Nuclei vulnerability scan against a deployed instance.
#
# https://docs.projectdiscovery.io/opensource/nuclei/ci-cd
#
# Nuclei is a template-driven scanner: thousands of community and official
# checks for CVEs, exposed panels and files, default credentials, misconfigured
# headers, technology fingerprints and more, run against a live HTTP target. It
# complements `tests/integration/specs/security/` rather than replacing it -
# those specs assert specific application behaviour (does another account's
# pin look like it never existed); this asks the broader, template-catalogue
# question (does anything here match a known-vulnerable pattern at all). Both
# are named in `docs/INTEGRATION_TESTS.md` as things the suite deliberately
# does not do itself.
#
# Same rules as `bin/run_integration_tests.sh`, for the same reasons:
#
#   - the target must be stated explicitly, and the script refuses to run
#     against a production hostname without an explicit override;
#   - manual only - never wired into a push/PR trigger, because a scan against
#     shared staging has to not collide with someone else using it;
#   - `--docker` needs nothing installed locally but Docker.
#
# Usage:
#   bin/run_nuclei_scan.sh --url https://s1.dev.urbanlens.org
#   bin/run_nuclei_scan.sh --url ... --docker              # no local install needed
#   bin/run_nuclei_scan.sh --url ... --fail-on-findings    # nonzero exit if anything matched
#   bin/run_nuclei_scan.sh --url ... -- -tags cve,exposure # pass through
#
# Anything after `--` is handed to `nuclei` unchanged.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/tests/integration/reports/nuclei"

BASE_URL="${UL_NUCLEI_BASE_URL:-${UL_E2E_BASE_URL:-}}"
SEVERITY="${UL_NUCLEI_SEVERITY:-info,low,medium,high,critical}"
EXTRA_EXCLUDE_TAGS="${UL_NUCLEI_EXCLUDE_TAGS:-}"
RATE_LIMIT="${UL_NUCLEI_RATE_LIMIT:-50}"
CONCURRENCY="${UL_NUCLEI_CONCURRENCY:-25}"
NUCLEI_VERSION="${UL_NUCLEI_VERSION:-latest}"
USE_DOCKER=0
UPDATE_TEMPLATES=1
FAIL_ON_FINDINGS=0
PASSTHROUGH=()

# Denial-of-service templates are excluded unconditionally - attempting it would impact
# our other infrastructure on the same machine.
BASE_EXCLUDE_TAGS="dos"

usage() {
	cat <<-EOF
		Run a Nuclei scan against a deployed instance.

		  --url URL              The deployment to scan (or set UL_NUCLEI_BASE_URL /
		                         UL_E2E_BASE_URL - the same variable the integration
		                         suite uses, so one env file covers both).
		  --severity LIST        Comma-separated severities (default: everything -
		                         "${SEVERITY}").
		  --exclude-tags LIST    Additional tags to skip, on top of the fixed "dos"
		                         exclusion.
		  --rate-limit N         Max requests/second (default: ${RATE_LIMIT}). Keep this
		                         conservative against a shared staging box - see the
		                         write-quota discussion in docs/INTEGRATION_TESTS.md for
		                         why "the burstiest client it will ever have" is a
		                         mistake worth avoiding twice.
		  --concurrency N        Parallel template executions (default: ${CONCURRENCY}).
		  --skip-update          Don't refresh the template catalogue first.
		  --fail-on-findings     Exit non-zero if anything matched. Off by default: a
		                         finding here is a lead to triage, not automatically a
		                         broken build - same posture as the audit tooling in
		                         docs/TOOLING.md.
		  --docker               Run the official projectdiscovery/nuclei image; needs
		                         only Docker, no local install.
		  --version VERSION      Pin the Nuclei/image version (default: ${NUCLEI_VERSION}).
		  -- ARGS...             Everything after this goes to \`nuclei\` unchanged.

		Reports land in tests/integration/reports/nuclei/ (JSON Lines and SARIF), the
		same tree the integration suite uses, so both are picked up by the same CI
		artifact upload.

		Refuses to run against a production hostname. Override with
		UL_NUCLEI_ALLOW_PRODUCTION=1 if you genuinely mean it.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--url)
			BASE_URL="$2"
			shift 2
			;;
		--severity)
			SEVERITY="$2"
			shift 2
			;;
		--exclude-tags)
			EXTRA_EXCLUDE_TAGS="$2"
			shift 2
			;;
		--rate-limit)
			RATE_LIMIT="$2"
			shift 2
			;;
		--concurrency)
			CONCURRENCY="$2"
			shift 2
			;;
		--skip-update)
			UPDATE_TEMPLATES=0
			shift
			;;
		--fail-on-findings)
			FAIL_ON_FINDINGS=1
			shift
			;;
		--docker)
			USE_DOCKER=1
			shift
			;;
		--version)
			NUCLEI_VERSION="$2"
			shift 2
			;;
		-h | --help)
			usage
			exit 0
			;;
		--)
			shift
			PASSTHROUGH=("$@")
			break
			;;
		*)
			echo "Unknown argument: $1" >&2
			usage >&2
			exit 2
			;;
	esac
done

if [[ -z "${BASE_URL}" ]]; then
	echo "error: no target. Pass --url https://your-staging-host, or set UL_NUCLEI_BASE_URL." >&2
	exit 2
fi

# Exact-hostname match, same logic as tests/integration/lib/env.ts's guard -
# scheme, path, query and port stripped, so "s1.dev.urbanlens.org" is never
# caught by an entry for "urbanlens.org".
hostname_of() {
	local url="$1"
	url="${url#*://}"
	url="${url%%/*}"
	url="${url%%\?*}"
	url="${url%%:*}"
	printf '%s' "${url,,}"
}

DEFAULT_PRODUCTION_HOSTS="urbanlens.org,www.urbanlens.org,app.urbanlens.org,urbanlens.com,www.urbanlens.com,app.urbanlens.com"
PRODUCTION_HOSTS="${UL_NUCLEI_PRODUCTION_HOSTS:-${DEFAULT_PRODUCTION_HOSTS}}"
ALLOW_PRODUCTION="${UL_NUCLEI_ALLOW_PRODUCTION:-0}"
TARGET_HOST="$(hostname_of "${BASE_URL}")"

IFS=',' read -ra _production_hosts <<<"${PRODUCTION_HOSTS}"
for _host in "${_production_hosts[@]}"; do
	_host="$(echo -n "${_host}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
	if [[ -n "${_host}" && "${TARGET_HOST}" == "${_host}" && "${ALLOW_PRODUCTION}" != "1" ]]; then
		cat >&2 <<-EOF
			error: refusing to scan "${TARGET_HOST}", which is listed in UL_NUCLEI_PRODUCTION_HOSTS.
			This fires real requests, including ones templates tag as intrusive, at whatever
			it is pointed at. Point --url at staging, or set UL_NUCLEI_ALLOW_PRODUCTION=1 if
			you genuinely mean it.
		EOF
		exit 2
	fi
done

EXCLUDE_TAGS="${BASE_EXCLUDE_TAGS}"
if [[ -n "${EXTRA_EXCLUDE_TAGS}" ]]; then
	EXCLUDE_TAGS="${EXCLUDE_TAGS},${EXTRA_EXCLUDE_TAGS}"
fi

mkdir -p "${OUT_DIR}"
JSONL_OUT="${OUT_DIR}/results.jsonl"
SARIF_OUT="${OUT_DIR}/results.sarif"
rm -f "${JSONL_OUT}" "${SARIF_OUT}"

# Common to both run modes; the docker branch below appends the same output
# flags pointed at the container's mounted path instead.
NUCLEI_ARGS=(
	-u "${BASE_URL}"
	-severity "${SEVERITY}"
	-exclude-tags "${EXCLUDE_TAGS}"
	-rate-limit "${RATE_LIMIT}"
	-c "${CONCURRENCY}"
	-retries 1
	-stats
)
if [[ ${UPDATE_TEMPLATES} -eq 1 ]]; then
	NUCLEI_ARGS+=(-update-templates)
fi

echo "Scanning ${BASE_URL} (severity: ${SEVERITY}; excluding tags: ${EXCLUDE_TAGS})"

if [[ ${USE_DOCKER} -eq 1 ]]; then
	IMAGE="projectdiscovery/nuclei:${NUCLEI_VERSION}"
	echo "Running in ${IMAGE}"
	# Output dir mounted so the reports land where the rest of this script (and
	# CI's artifact upload) expects them. No template cache mount: the runners
	# this is expected to run on (a fresh CI job, or chiron) don't keep one
	# between runs anyway, so a bind mount would only add an unverified path
	# assumption for no benefit.
	docker run --rm \
		-v "${OUT_DIR}:/output" \
		"${IMAGE}" \
		"${NUCLEI_ARGS[@]}" \
		-jsonl -o /output/results.jsonl \
		-sarif-export /output/results.sarif \
		${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
else
	command -v nuclei >/dev/null 2>&1 || {
		echo "error: nuclei is not on PATH. Install it (https://docs.projectdiscovery.io/opensource/nuclei/install) or re-run with --docker." >&2
		exit 1
	}
	nuclei "${NUCLEI_ARGS[@]}" \
		-jsonl -o "${JSONL_OUT}" \
		-sarif-export "${SARIF_OUT}" \
		${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
fi

FINDING_COUNT=0
if [[ -f "${JSONL_OUT}" ]]; then
	FINDING_COUNT=$(wc -l <"${JSONL_OUT}" | tr -d '[:space:]')
fi

echo ""
echo "${FINDING_COUNT} finding(s). Reports: ${JSONL_OUT}, ${SARIF_OUT}"

if [[ ${FINDING_COUNT} -gt 0 && ${FAIL_ON_FINDINGS} -eq 1 ]]; then
	echo "error: --fail-on-findings set and ${FINDING_COUNT} finding(s) were reported." >&2
	exit 1
fi
