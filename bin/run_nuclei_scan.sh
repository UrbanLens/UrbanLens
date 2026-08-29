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
#   bin/run_nuclei_scan.sh --url ... --accounts-file /tmp/e2e.json --all-tiers
#   bin/run_nuclei_scan.sh --url ... -- -tags cve,exposure # pass through
#
# Anything after `--` is handed to `nuclei` unchanged.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE_DIR="${REPO_ROOT}/tests/integration"
OUT_DIR="${SUITE_DIR}/reports/nuclei"

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
ALL_TIERS=0
PASSTHROUGH=()

# Cleaned up on exit: a generated secret-file, which holds a live API key or
# session cookie. `|| true` per path, not once for the whole loop - one path
# that fails to remove (e.g. root-owned leftovers from a container run) must
# never stop the rest from being attempted, since under `set -e` a failing
# command inside an EXIT trap aborts the trap itself. That exact gap is how
# an earlier version of this script left real credentials behind in /tmp:
# a container-written path failed first, and the loop never reached the
# secret-files queued after it.
CLEANUP_PATHS=()
cleanup() {
	local p
	for p in ${CLEANUP_PATHS[@]+"${CLEANUP_PATHS[@]}"}; do
		rm -rf "${p}" 2>/dev/null || true
	done
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
		  --all-tiers            With --accounts-file: scan four times - unauthenticated,
		                         the restricted-scope API key, the full-scope API key,
		                         and a real signed-in session (cookie auth, covering the
		                         HTML/HTMX dashboard surface the API key cannot reach at
		                         all - see docs/INTEGRATION_TESTS.md). Each writes to its
		                         own subdirectory under reports/nuclei/. A tier that
		                         can't be set up (no restricted key provisioned, no Node
		                         for the session login) is skipped with a warning rather
		                         than failing the run.
		  --secret-file PATH     Pass a hand-written Nuclei secret-file instead of
		                         generating one - see
		                         https://docs.projectdiscovery.io/opensource/nuclei/authenticated-scans.
		                         Mutually exclusive with --accounts-file/--all-tiers.
		  --fail-on-findings     Exit non-zero if anything matched, across every tier
		                         run. Off by default: a finding here is a lead to
		                         triage, not automatically a broken build - same
		                         posture as the audit tooling in docs/TOOLING.md.
		  --docker               Run the official projectdiscovery/nuclei image; needs
		                         only Docker, no local install.
		  --version VERSION      Pin the Nuclei/image version (default: ${NUCLEI_VERSION}).
		  -- ARGS...             Everything after this goes to \`nuclei\` unchanged.

		Reports land in tests/integration/reports/nuclei/ (JSON Lines and SARIF; one
		subdirectory per tier under --all-tiers), the same tree the integration suite
		uses, so both are picked up by the same CI artifact upload.

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
		--all-tiers)
			ALL_TIERS=1
			shift
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

if [[ -n "${SECRET_FILE}" && ( -n "${ACCOUNTS_FILE}" || ${ALL_TIERS} -eq 1 ) ]]; then
	echo "error: --secret-file is mutually exclusive with --accounts-file/--all-tiers." >&2
	exit 2
fi

if [[ ${ALL_TIERS} -eq 1 && -z "${ACCOUNTS_FILE}" ]]; then
	echo "error: --all-tiers needs --accounts-file (or UL_NUCLEI_ACCOUNTS_FILE/UL_E2E_ACCOUNTS_FILE)." >&2
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

if [[ -n "${SECRET_FILE}" ]]; then
	[[ -f "${SECRET_FILE}" ]] || {
		echo "error: --secret-file ${SECRET_FILE} does not exist." >&2
		exit 2
	}
elif [[ -n "${ACCOUNTS_FILE}" ]]; then
	[[ -f "${ACCOUNTS_FILE}" ]] || {
		echo "error: --accounts-file ${ACCOUNTS_FILE} does not exist." >&2
		exit 2
	}
fi

# Emits a bearertoken secret-file scoped to TARGET_HOST for the named field
# ("api_key" or "restricted_api_key") of the primary account in
# ACCOUNTS_FILE. Prints the generated path on success; exits non-zero (with a
# reason on stderr) when that account has no such key.
generate_bearer_secret_file() {
	local field="$1"
	local out
	out="$(mktemp)"
	chmod 600 "${out}"
	if ! python3 - "${ACCOUNTS_FILE}" "${TARGET_HOST}" "${field}" >"${out}" <<-'PY'
		import json
		import sys

		manifest_path, target_host, field = sys.argv[1], sys.argv[2], sys.argv[3]
		with open(manifest_path, encoding="utf-8") as f:
		    manifest = json.load(f)
		accounts = {a["role"]: a for a in manifest.get("accounts", [])}
		primary = accounts.get("primary")
		token = primary.get(field) if primary else None
		if not token:
		    sys.exit(f"the primary account in {manifest_path} has no {field}")
		print("static:")
		print("  - type: bearertoken")
		print("    domains:")
		print(f"      - {json.dumps(target_host)}")
		print(f"    token: {json.dumps(token)}")
	PY
	then
		rm -f "${out}"
		return 1
	fi
	printf '%s' "${out}"
}

# Signs in as the primary account through a real browser (reusing
# tests/integration/setup/auth.setup.ts - Django's CSRF-protected login form
# is not something worth reimplementing in bash) and turns the resulting
# session into a cookie secret-file scoped to TARGET_HOST. Needs Node; prints
# the generated path on success.
mint_session_secret_file() {
	if ! command -v npm >/dev/null 2>&1; then
		echo "no npm on PATH - the session tier needs Node to drive a real login" >&2
		return 1
	fi
	# A bare (subshell) statement here would let a real failure inside it (a
	# broken login, a missing browser download) trigger set -e and kill the
	# whole script before the `if secret_file=$(mint_session_secret_file)`
	# at the call site ever gets a chance to catch it - wrapping it in `||`
	# is what makes this tier skippable like the other three.
	local login_rc=0
	(
		cd "${SUITE_DIR}"
		if [[ ! -d node_modules ]]; then
			if [[ -f package-lock.json ]]; then
				npm ci --no-audit --no-fund
			else
				npm install --no-audit --no-fund
			fi
		fi
		npx playwright install --with-deps chromium
		UL_E2E_BASE_URL="${BASE_URL}" UL_E2E_ACCOUNTS_FILE="${ACCOUNTS_FILE}" npx playwright test --project=setup --grep "sign in as primary"
	) 1>&2 || login_rc=$?
	if [[ ${login_rc} -ne 0 ]]; then
		echo "sign-in failed (exit ${login_rc})" >&2
		return 1
	fi

	local state_file="${SUITE_DIR}/.auth/primary.json"
	if [[ ! -f "${state_file}" ]]; then
		echo "sign-in did not produce ${state_file}" >&2
		return 1
	fi

	local out
	out="$(mktemp)"
	chmod 600 "${out}"
	if ! python3 - "${state_file}" "${TARGET_HOST}" >"${out}" <<-'PY'
		import json
		import sys

		state_path, target_host = sys.argv[1], sys.argv[2]
		with open(state_path, encoding="utf-8") as f:
		    state = json.load(f)
		wanted = {"sessionid", "csrftoken"}
		cookies = [c for c in state.get("cookies", []) if c["name"] in wanted]
		if not any(c["name"] == "sessionid" for c in cookies):
		    sys.exit(f"no sessionid cookie in {state_path}")
		print("static:")
		print("  - type: cookie")
		print("    domains:")
		print(f"      - {json.dumps(target_host)}")
		print("    cookies:")
		for c in cookies:
		    print(f"      - key: {json.dumps(c['name'])}")
		    print(f"        value: {json.dumps(c['value'])}")
	PY
	then
		rm -f "${out}"
		return 1
	fi
	printf '%s' "${out}"
}

# Common to every run mode and every tier; the docker branch below appends
# the same output (and, per-tier, secret-file) flags pointed at the
# container's mounted paths instead.
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

IMAGE="projectdiscovery/nuclei:${NUCLEI_VERSION}"

if [[ ${USE_DOCKER} -eq 0 && ${UPDATE_TEMPLATES} -eq 1 ]]; then
	command -v nuclei >/dev/null 2>&1 || {
		echo "error: nuclei is not on PATH. Install it (https://docs.projectdiscovery.io/opensource/nuclei/install) or re-run with --docker." >&2
		exit 1
	}
	echo "Updating the template catalogue..."
	nuclei -update-templates
fi

# Runs one full scan for one tier, writing to reports/nuclei/<tier>/ and
# recording its finding count in TIER_COUNTS / TOTAL_FINDINGS.
TIER_COUNTS=()
TOTAL_FINDINGS=0
run_scan_for_tier() {
	local tier_name="$1" secret_file="$2"
	local tier_out_dir="${OUT_DIR}/${tier_name}"
	mkdir -p "${tier_out_dir}"
	local jsonl_out="${tier_out_dir}/results.jsonl" sarif_out="${tier_out_dir}/results.sarif"
	rm -f "${jsonl_out}" "${sarif_out}"

	echo ""
	echo "=== ${tier_name} ==="

	if [[ ${USE_DOCKER} -eq 1 ]]; then
		# No shared template-cache mount across tiers: nuclei runs as root
		# inside the container, so anything it writes to a bind-mounted host
		# directory comes out root-owned, and a non-root cleanup can't remove
		# it afterwards - tried this, found it the hard way (see the EXIT
		# trap above). Each tier re-downloads the catalogue instead; slower,
		# but simple and it can never leave root-owned junk behind.
		local secret_mount=() secret_args=()
		if [[ -n "${secret_file}" ]]; then
			secret_mount=(-v "${secret_file}:/secrets/secret-file.yaml:ro")
			secret_args=(-secret-file /secrets/secret-file.yaml)
		fi
		docker run --rm \
			-v "${tier_out_dir}:/output" \
			${secret_mount[@]+"${secret_mount[@]}"} \
			"${IMAGE}" \
			"${NUCLEI_ARGS[@]}" \
			${secret_args[@]+"${secret_args[@]}"} \
			-jsonl -o /output/results.jsonl \
			-sarif-export /output/results.sarif \
			${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
	else
		local secret_args=()
		if [[ -n "${secret_file}" ]]; then
			secret_args=(-secret-file "${secret_file}")
		fi
		nuclei "${NUCLEI_ARGS[@]}" \
			${secret_args[@]+"${secret_args[@]}"} \
			-jsonl -o "${jsonl_out}" \
			-sarif-export "${sarif_out}" \
			${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
	fi

	local count=0
	[[ -f "${jsonl_out}" ]] && count=$(wc -l <"${jsonl_out}" | tr -d '[:space:]')
	echo "${tier_name}: ${count} finding(s)"
	TIER_COUNTS+=("${tier_name}=${count}")
	TOTAL_FINDINGS=$((TOTAL_FINDINGS + count))
}

echo "Scanning ${BASE_URL} (severity: ${SEVERITY}; excluding tags: ${EXCLUDE_TAGS})"
if [[ ${USE_DOCKER} -eq 1 ]]; then
	echo "Running in ${IMAGE}"
fi

if [[ ${ALL_TIERS} -eq 1 ]]; then
	run_scan_for_tier "unauthenticated" ""

	# The reason for a skip is printed by the generator itself (stderr), not
	# repeated here - the function's stdout, captured into secret_file, is
	# the secret-file path on success and empty on failure.
	if secret_file=$(generate_bearer_secret_file restricted_api_key); then
		CLEANUP_PATHS+=("${secret_file}")
		run_scan_for_tier "apikey-restricted" "${secret_file}"
	else
		echo "skipping apikey-restricted" >&2
	fi

	if secret_file=$(generate_bearer_secret_file api_key); then
		CLEANUP_PATHS+=("${secret_file}")
		run_scan_for_tier "apikey-full" "${secret_file}"
	else
		echo "skipping apikey-full" >&2
	fi

	if secret_file=$(mint_session_secret_file); then
		CLEANUP_PATHS+=("${secret_file}")
		run_scan_for_tier "session" "${secret_file}"
	else
		echo "skipping session" >&2
	fi

	echo ""
	echo "=== Summary ==="
	for entry in "${TIER_COUNTS[@]}"; do
		echo "  ${entry}"
	done
	echo "  total=${TOTAL_FINDINGS}"

	if [[ ${TOTAL_FINDINGS} -gt 0 && ${FAIL_ON_FINDINGS} -eq 1 ]]; then
		echo "error: --fail-on-findings set and ${TOTAL_FINDINGS} finding(s) were reported across all tiers." >&2
		exit 1
	fi
	exit 0
fi

# Single-tier path (the default): one secret-file at most, results at the
# flat reports/nuclei/ location rather than a per-tier subdirectory.
SINGLE_SECRET_FILE="${SECRET_FILE}"
if [[ -z "${SINGLE_SECRET_FILE}" && -n "${ACCOUNTS_FILE}" ]]; then
	SINGLE_SECRET_FILE="$(generate_bearer_secret_file api_key)"
	CLEANUP_PATHS+=("${SINGLE_SECRET_FILE}")
	echo "Authenticating as the primary account from ${ACCOUNTS_FILE} (Bearer, scoped to ${TARGET_HOST})"
fi

mkdir -p "${OUT_DIR}"
JSONL_OUT="${OUT_DIR}/results.jsonl"
SARIF_OUT="${OUT_DIR}/results.sarif"
rm -f "${JSONL_OUT}" "${SARIF_OUT}"

if [[ ${USE_DOCKER} -eq 1 ]]; then
	SECRET_MOUNT=()
	SECRET_ARGS=()
	if [[ -n "${SINGLE_SECRET_FILE}" ]]; then
		SECRET_MOUNT=(-v "${SINGLE_SECRET_FILE}:/secrets/secret-file.yaml:ro")
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
	SECRET_ARGS=()
	if [[ -n "${SINGLE_SECRET_FILE}" ]]; then
		SECRET_ARGS=(-secret-file "${SINGLE_SECRET_FILE}")
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
