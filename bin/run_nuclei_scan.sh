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
#   bin/run_nuclei_scan.sh --url ... --accounts-file /tmp/e2e.json  # authenticated
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
ACCOUNTS_FILE="${UL_NUCLEI_ACCOUNTS_FILE:-${UL_E2E_ACCOUNTS_FILE:-}}"
SECRET_FILE="${UL_NUCLEI_SECRET_FILE:-}"
USE_DOCKER=0
UPDATE_TEMPLATES=1
FAIL_ON_FINDINGS=0
PASSTHROUGH=()
GENERATED_SECRET_FILE=""
cleanup() {
	[[ -n "${GENERATED_SECRET_FILE}" && -f "${GENERATED_SECRET_FILE}" ]] && rm -f "${GENERATED_SECRET_FILE}"
}
trap cleanup EXIT

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
		  --accounts-file PATH   Manifest written by \`manage.py provision_integration_env\`
		                         (or set UL_NUCLEI_ACCOUNTS_FILE / UL_E2E_ACCOUNTS_FILE).
		                         Authenticates every request as the primary account
		                         (Authorization: Bearer <api_key>), so templates that
		                         need a signed-in request can reach past the login
		                         wall. Generates a Nuclei secret-file scoped to the
		                         target's own hostname; never reused against another.
		  --secret-file PATH     Pass a hand-written Nuclei secret-file instead of
		                         generating one - see
		                         https://docs.projectdiscovery.io/opensource/nuclei/authenticated-scans.
		                         Mutually exclusive with --accounts-file.
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
		--accounts-file)
			ACCOUNTS_FILE="$2"
			shift 2
			;;
		--secret-file)
			SECRET_FILE="$2"
			shift 2
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

if [[ -n "${ACCOUNTS_FILE}" && -n "${SECRET_FILE}" ]]; then
	echo "error: --accounts-file and --secret-file are mutually exclusive." >&2
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

# A target nuclei can't actually reach produces the same symptom as nothing
# being wrong: zero matches, exit 0, no error. Fail loudly here instead of
# discovering it by eyeballing a suspiciously-empty report - this is exactly
# how a real bug in this script (see below) first surfaced as "0 findings".
if command -v curl >/dev/null 2>&1; then
	curl_status=0
	curl -sS -o /dev/null --max-time 15 "${BASE_URL}" || curl_status=$?
	if [[ ${curl_status} -ne 0 ]]; then
		echo "error: could not reach ${BASE_URL} at all (curl exit ${curl_status}). Check the URL, VPN/network access, and DNS before blaming Nuclei for an empty report." >&2
		exit 1
	fi
else
	echo "warning: curl not found, skipping the reachability preflight." >&2
fi

if [[ -n "${ACCOUNTS_FILE}" ]]; then
	[[ -f "${ACCOUNTS_FILE}" ]] || {
		echo "error: --accounts-file ${ACCOUNTS_FILE} does not exist." >&2
		exit 2
	}
	GENERATED_SECRET_FILE="$(mktemp)"
	chmod 600 "${GENERATED_SECRET_FILE}"
	# Bearer-token auth only - it unlocks the external API surface
	# (docs/EXTERNAL_API.md: every credential is `Authorization: Bearer <token>`).
	# Session-cookie auth for the HTML/dashboard surface needs a real Django
	# login (CSRF token, form POST, redirect chain) rather than a static
	# secret, which tests/integration/setup/auth.setup.ts already does
	# properly via Playwright - reuse that instead of re-implementing a login
	# flow in bash. See docs/INTEGRATION_TESTS.md.
	python3 - "${ACCOUNTS_FILE}" "${TARGET_HOST}" >"${GENERATED_SECRET_FILE}" <<-'PY'
		import json
		import sys

		manifest_path, target_host = sys.argv[1], sys.argv[2]
		with open(manifest_path, encoding="utf-8") as f:
		    manifest = json.load(f)
		accounts = {a["role"]: a for a in manifest.get("accounts", [])}
		primary = accounts.get("primary")
		if not primary or not primary.get("api_key"):
		    sys.exit(f"error: {manifest_path} has no primary account with an api_key")
		print("static:")
		print("  - type: bearertoken")
		print("    domains:")
		print(f"      - {json.dumps(target_host)}")
		print(f"    token: {json.dumps(primary['api_key'])}")
	PY
	SECRET_FILE="${GENERATED_SECRET_FILE}"
	echo "Authenticating as the primary account from ${ACCOUNTS_FILE} (Bearer, scoped to ${TARGET_HOST})"
elif [[ -n "${SECRET_FILE}" ]]; then
	[[ -f "${SECRET_FILE}" ]] || {
		echo "error: --secret-file ${SECRET_FILE} does not exist." >&2
		exit 2
	}
fi

mkdir -p "${OUT_DIR}"
JSONL_OUT="${OUT_DIR}/results.jsonl"
SARIF_OUT="${OUT_DIR}/results.sarif"
rm -f "${JSONL_OUT}" "${SARIF_OUT}"

# Common to both run modes; the docker branch below appends the same output
# (and, if generated, secret-file) flags pointed at the container's mounted
# paths instead.
#
# Deliberately NOT combined with -update-templates: that flag is a one-shot
# maintenance action in nuclei - passed alongside -u it updates the template
# catalogue, exits 0, and never scans anything. No error, no warning, just a
# report with nothing in it. Confirmed against a real run: identical flags
# minus -update-templates went from 0 matches to a real scan of 10,689
# templates. Templates are refreshed as a separate step below instead.
NUCLEI_ARGS=(
	-u "${BASE_URL}"
	-severity "${SEVERITY}"
	-exclude-tags "${EXCLUDE_TAGS}"
	-rate-limit "${RATE_LIMIT}"
	-c "${CONCURRENCY}"
	-retries 1
	-stats
)

echo "Scanning ${BASE_URL} (severity: ${SEVERITY}; excluding tags: ${EXCLUDE_TAGS})"

if [[ ${USE_DOCKER} -eq 1 ]]; then
	IMAGE="projectdiscovery/nuclei:${NUCLEI_VERSION}"
	echo "Running in ${IMAGE}"
	# Output dir mounted so the reports land where the rest of this script (and
	# CI's artifact upload) expects them. No template cache mount: the runners
	# this is expected to run on (a fresh CI job, or chiron) don't keep one
	# between runs anyway, and a container with no existing template directory
	# installs the catalogue fresh on every run regardless of -update-templates
	# - so there is nothing useful for that flag to do here even for --docker.
	SECRET_MOUNT=()
	SECRET_ARGS=()
	if [[ -n "${SECRET_FILE}" ]]; then
		SECRET_MOUNT=(-v "${SECRET_FILE}:/secrets/secret-file.yaml:ro")
		SECRET_ARGS=(-secret-file /secrets/secret-file.yaml)
	fi
	docker run --rm \
		-v "${OUT_DIR}:/output" \
		${SECRET_MOUNT[@]+"${SECRET_MOUNT[@]}"} \
		"${IMAGE}" \
		"${NUCLEI_ARGS[@]}" \
		${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
		-jsonl -o /output/results.jsonl \
		-sarif-export /output/results.sarif \
		${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
else
	command -v nuclei >/dev/null 2>&1 || {
		echo "error: nuclei is not on PATH. Install it (https://docs.projectdiscovery.io/opensource/nuclei/install) or re-run with --docker." >&2
		exit 1
	}
	if [[ ${UPDATE_TEMPLATES} -eq 1 ]]; then
		echo "Updating the template catalogue..."
		nuclei -update-templates
	fi
	SECRET_ARGS=()
	if [[ -n "${SECRET_FILE}" ]]; then
		SECRET_ARGS=(-secret-file "${SECRET_FILE}")
	fi
	nuclei "${NUCLEI_ARGS[@]}" \
		${SECRET_ARGS[@]+"${SECRET_ARGS[@]}"} \
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
