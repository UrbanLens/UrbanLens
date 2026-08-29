#!/usr/bin/env bash
#
# Run sqlmap against a deployed instance's own OpenAPI-published external API,
# plus a crawl of the HTML/HTMX dashboard under a real session.
#
# https://github.com/sqlmapproject/sqlmap
#
# Nuclei (bin/run_nuclei_scan.sh) answers "does this deployment match a known
# vulnerable pattern"; the pytest suite's `security/` specs assert specific
# application behaviour. Neither actually tests for SQL injection - sqlmap
# does, and it EXPLOITS rather than merely detects: a confirmed finding here
# came from sqlmap sending real payloads through real parameters into the
# real database, not from matching a signature.
#
# That difference is exactly why this wrapper is more restrictive than
# run_nuclei_scan.sh in two ways nuclei does not need to be:
#
#   - target scope is an ALLOWLIST of disposable dev-container hosts, not a
#     denylist of production ones. staging.urbanlens.org is deliberately not
#     on that list - it is not a throwaway database, and this is a tool that
#     actively exploits what it finds (OR-based payloads at --risk=3 can
#     rewrite an UPDATE/DELETE's WHERE clause to match every row; stacked
#     queries can run arbitrary follow-up SQL). Only run this against
#     something you can rebuild from nothing.
#   - a fixed set of sqlmap flags - the ones that go past confirming an
#     injection into OS command execution, arbitrary file read/write, or an
#     interactive shell that bypasses --batch entirely - are refused
#     unconditionally, however they are passed. There is no opt-in for them
#     in this wrapper; run sqlmap by hand, outside it, if you mean to go that
#     far on a target you control.
#
# Otherwise deliberately permissive by default (full --risk/--level/--technique,
# matching Nuclei's "exclude only what is unconditionally unsafe" posture) -
# safe here specifically because the target is required to be disposable.
#
# Same rules as run_nuclei_scan.sh beyond that: manual only, never wired into
# a push/PR trigger or bundled into integration.yml's own dispatch; --docker
# is not offered because sqlmap has no compiled dependencies and pip-installs
# identically everywhere `bin/install_sqlmap.py` can reach a Python interpreter.
#
# Usage:
#   bin/run_sqlmap_scan.sh --url https://s1.dev.urbanlens.org
#   bin/run_sqlmap_scan.sh --url ... --accounts-file /tmp/e2e.json
#   bin/run_sqlmap_scan.sh --url ... --accounts-file /tmp/e2e.json --all-tiers
#   bin/run_sqlmap_scan.sh --url ... --fail-on-findings
#   bin/run_sqlmap_scan.sh --url ... -- --skip-waf --random-agent  # pass through
#
# Anything after `--` is handed to `sqlmap` unchanged, for every tier, except
# the flags this wrapper refuses outright (see HARD_BLOCKED_FLAGS below).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE_DIR="${REPO_ROOT}/tests/integration"
OUT_DIR="${SUITE_DIR}/reports/sqlmap"

# The schema this wrapper points sqlmap's own --openapi target-derivation at.
# Must match tests/contract/schema_source.py's SCHEMA_PATH - both read the
# same published document, and a change to one without the other would go
# unnoticed until a run against a real deployment came back suspiciously
# empty.
SCHEMA_PATH="/dashboard/api/external/v1/schema/"

BASE_URL="${UL_SQLMAP_BASE_URL:-${UL_E2E_BASE_URL:-}}"
RISK="${UL_SQLMAP_RISK:-3}"
LEVEL="${UL_SQLMAP_LEVEL:-5}"
TECHNIQUE="${UL_SQLMAP_TECHNIQUE:-BEUSTQ}"
THREADS="${UL_SQLMAP_THREADS:-4}"
DELAY="${UL_SQLMAP_DELAY:-0}"
CRAWL_DEPTH="${UL_SQLMAP_CRAWL_DEPTH:-3}"
CRAWL_EXCLUDE="${UL_SQLMAP_CRAWL_EXCLUDE:-(?i)logout}"
ACCOUNTS_FILE="${UL_SQLMAP_ACCOUNTS_FILE:-${UL_E2E_ACCOUNTS_FILE:-}}"
ALL_TIERS=0
FAIL_ON_FINDINGS=0
PASSTHROUGH=()

# Cleaned up on exit: generated sqlmap config files hold a live API key or
# session cookie in their [Request] section. Same `|| true` per-path pattern
# as run_nuclei_scan.sh's secret-file cleanup, for the same reason - one path
# failing to remove must never stop the rest from being attempted.
CLEANUP_PATHS=()
cleanup() {
	local p
	for p in ${CLEANUP_PATHS[@]+"${CLEANUP_PATHS[@]}"}; do
		rm -f "${p}" 2>/dev/null || true
	done
}
trap cleanup EXIT

# Flags this wrapper refuses outright, however they arrive (bare, via `--`
# passthrough, or with a `=value` suffix). All of them go past confirming an
# injection exists into operating-system command execution, arbitrary
# filesystem access on the database host, Windows registry access, or an
# interactive shell/SQL prompt that bypasses --batch entirely - out of scope
# for an automated scan regardless of how disposable the target is.
HARD_BLOCKED_FLAGS=(
	--os-shell --os-pwn --os-cmd --os-smbrelay --os-bof --priv-esc
	--file-read --file-write --file-dest
	--sql-shell --udf-inject
	--reg-read --reg-add --reg-del --reg-key --reg-value --reg-data --reg-type
)

refuse_hard_blocked_flags() {
	local offending=() arg flag blocked
	for arg in ${1+"$@"}; do
		flag="${arg%%=*}"
		for blocked in "${HARD_BLOCKED_FLAGS[@]}"; do
			[[ "${flag}" == "${blocked}" ]] && offending+=("${arg}")
		done
	done
	if [[ ${#offending[@]} -gt 0 ]]; then
		echo "error: refusing to run - the following flag(s) are permanently blocked by this wrapper:" >&2
		printf '  %s\n' "${offending[@]}" >&2
		echo "See the header of bin/run_sqlmap_scan.sh for why. Run sqlmap by hand, outside this wrapper, if you genuinely mean to do this." >&2
		exit 2
	fi
}

usage() {
	cat <<-EOF
		Run sqlmap against a deployed instance's published external API (via its own
		OpenAPI schema) plus a crawl of the HTML/HTMX dashboard under a real session.

		  --url URL              The deployment to scan (or set UL_SQLMAP_BASE_URL /
		                         UL_E2E_BASE_URL). Must resolve to an allowed
		                         dev-container host - see UL_SQLMAP_ALLOWED_HOSTS below.
		  --accounts-file PATH   Manifest written by \`manage.py provision_integration_env\`
		                         (or set UL_SQLMAP_ACCOUNTS_FILE / UL_E2E_ACCOUNTS_FILE).
		                         Without it, the default (non---all-tiers) run is
		                         unauthenticated.
		  --all-tiers            With --accounts-file: scan four times - unauthenticated,
		                         the restricted-scope API key, the full-scope API key
		                         (each via sqlmap's own --openapi target derivation
		                         against the published schema), and a real signed-in
		                         session (--crawl/--forms against the HTML/HTMX
		                         dashboard, which the API key cannot reach at all - see
		                         docs/INTEGRATION_TESTS.md). A tier that can't be set up
		                         (no restricted key provisioned, no Node for the session
		                         login) is skipped with a warning rather than failing.
		  --risk N               sqlmap --risk, 1-3 (default ${RISK}). Deliberately
		                         permissive: --risk=3's OR-based payloads can rewrite an
		                         UPDATE/DELETE's WHERE clause, which is only acceptable
		                         because the target is required to be disposable.
		  --level N              sqlmap --level, 1-5 (default ${LEVEL}). 5 also tests
		                         cookies, User-Agent, Referer and Host - the widest
		                         surface sqlmap will cover on its own.
		  --technique STR        sqlmap --technique (default "${TECHNIQUE}", sqlmap's own
		                         default - includes stacked queries). Narrow this
		                         (e.g. BEUQ) for a read-only-leaning run.
		  --threads N            sqlmap --threads (default ${THREADS}). Always 1 for the
		                         session tier regardless of this value - sqlmap refuses
		                         to combine --csrf-token with --threads > 1.
		  --delay N              sqlmap --delay, seconds between requests (default ${DELAY}).
		  --crawl-depth N        Session tier's --crawl depth (default ${CRAWL_DEPTH}).
		  --fail-on-findings     Exit non-zero if sqlmap confirmed an injection in any
		                         tier. Off by default - same posture as run_nuclei_scan.sh
		                         and the audit tooling in docs/TOOLING.md: a finding here
		                         is a lead to triage, not automatically a broken build.
		  -- ARGS...             Everything after this goes to \`sqlmap\`, for every tier,
		                         except the flags this wrapper refuses - see the header
		                         of this script.

		Reports land in tests/integration/reports/sqlmap/ (sqlmap's own --output-dir
		tree plus a report.json per tier under --all-tiers), the same tree the
		integration suite and Nuclei use, so all three are picked up by one CI
		artifact upload.

		Refuses to run against anything but an allowed dev-container host
		(UL_SQLMAP_ALLOWED_HOSTS, default ".dev.urbanlens.org,localhost,127.0.0.1").
		staging.urbanlens.org is NOT on that list on purpose - override with
		UL_SQLMAP_ALLOW_ANY_HOST=1 only once you have confirmed the target database is
		genuinely disposable.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--url)
			BASE_URL="$2"
			shift 2
			;;
		--accounts-file)
			ACCOUNTS_FILE="$2"
			shift 2
			;;
		--all-tiers)
			ALL_TIERS=1
			shift
			;;
		--risk)
			RISK="$2"
			shift 2
			;;
		--level)
			LEVEL="$2"
			shift 2
			;;
		--technique)
			TECHNIQUE="$2"
			shift 2
			;;
		--threads)
			THREADS="$2"
			shift 2
			;;
		--delay)
			DELAY="$2"
			shift 2
			;;
		--crawl-depth)
			CRAWL_DEPTH="$2"
			shift 2
			;;
		--fail-on-findings)
			FAIL_ON_FINDINGS=1
			shift
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

refuse_hard_blocked_flags ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}

if [[ -z "${BASE_URL}" ]]; then
	echo "error: no target. Pass --url https://your-dev-container, or set UL_SQLMAP_BASE_URL." >&2
	exit 2
fi
BASE_URL="${BASE_URL%/}"

if [[ ${ALL_TIERS} -eq 1 && -z "${ACCOUNTS_FILE}" ]]; then
	echo "error: --all-tiers needs --accounts-file (or UL_SQLMAP_ACCOUNTS_FILE/UL_E2E_ACCOUNTS_FILE)." >&2
	exit 2
fi

if [[ -n "${ACCOUNTS_FILE}" && ! -f "${ACCOUNTS_FILE}" ]]; then
	echo "error: --accounts-file ${ACCOUNTS_FILE} does not exist." >&2
	exit 2
fi

# Exact-hostname/suffix match, same string handling as run_nuclei_scan.sh's
# guard - scheme, path, query and port stripped.
hostname_of() {
	local url="$1"
	url="${url#*://}"
	url="${url%%/*}"
	url="${url%%\?*}"
	url="${url%%:*}"
	printf '%s' "${url,,}"
}

# An ALLOWLIST rather than run_nuclei_scan.sh's denylist - see the header of
# this script for why sqlmap needs the stricter default. A leading "." on an
# entry matches it as a domain suffix (so ".dev.urbanlens.org" allows
# "s1.dev.urbanlens.org" but not "dev.urbanlens.org" itself); anything else
# must match the hostname exactly.
DEFAULT_ALLOWED_HOSTS=".dev.urbanlens.org,localhost,127.0.0.1"
ALLOWED_HOSTS="${UL_SQLMAP_ALLOWED_HOSTS:-${DEFAULT_ALLOWED_HOSTS}}"
ALLOW_ANY_HOST="${UL_SQLMAP_ALLOW_ANY_HOST:-0}"
TARGET_HOST="$(hostname_of "${BASE_URL}")"

host_is_allowed() {
	local host="$1" pattern
	IFS=',' read -ra _patterns <<<"${ALLOWED_HOSTS}"
	for pattern in "${_patterns[@]}"; do
		pattern="$(echo -n "${pattern}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
		[[ -z "${pattern}" ]] && continue
		if [[ "${pattern}" == .* ]]; then
			[[ "${host}" == *"${pattern}" ]] && return 0
		elif [[ "${host}" == "${pattern}" ]]; then
			return 0
		fi
	done
	return 1
}

if [[ "${ALLOW_ANY_HOST}" != "1" ]] && ! host_is_allowed "${TARGET_HOST}"; then
	cat >&2 <<-EOF
		error: refusing to run sqlmap against "${TARGET_HOST}".

		This wrapper only runs against a short allowlist of disposable dev-container
		hosts by default (UL_SQLMAP_ALLOWED_HOSTS, currently "${ALLOWED_HOSTS}") -
		sqlmap actively exploits an injection rather than just detecting one, which is
		only safe against a database that can be rebuilt from nothing.
		staging.urbanlens.org is deliberately NOT on that list.

		Point --url at a dev container (e.g. https://s1.dev.urbanlens.org), or set
		UL_SQLMAP_ALLOW_ANY_HOST=1 if you have genuinely confirmed this target's
		database is disposable and mean to scan it anyway.
	EOF
	exit 2
fi

# A target sqlmap can't reach at all produces a report that looks identical to
# a hardened deployment - the "0 findings, no error" trap run_nuclei_scan.sh's
# own history warns about. Fail loudly here instead.
if command -v curl >/dev/null 2>&1; then
	curl_status=0
	curl -sS -o /dev/null --max-time 15 "${BASE_URL}" || curl_status=$?
	if [[ ${curl_status} -ne 0 ]]; then
		echo "error: could not reach ${BASE_URL} at all (curl exit ${curl_status}). Check the URL and network access before blaming sqlmap for an empty report." >&2
		exit 1
	fi
else
	echo "warning: curl not found, skipping the reachability preflight." >&2
fi

echo "Ensuring a pinned, hash-verified sqlmap is installed..."
SQLMAP_BIN="$(python3 "${REPO_ROOT}/bin/install_sqlmap.py")"

SCHEMA_URL="${BASE_URL}${SCHEMA_PATH}?format=json"

# All 17 section headers sqlmap's own `--save` writes, even the ones this
# wrapper never populates - `-c` fails outright ("missing a mandatory section")
# if any is absent, confirmed against a real 1.10.8 install.
_INI_SECTIONS=(Target Request Optimization Injection Detection Techniques Fingerprint Enumeration Brute "User-defined function" "File system" Takeover Windows General Miscellaneous Hidden API)

# Emits a minimal sqlmap config file carrying only a live credential, so it
# never appears in argv/process-list (the same reason run_nuclei_scan.sh
# builds a -secret-file rather than passing a bearer token as a CLI flag).
# Prints the generated path on success; exits non-zero (reason on stderr)
# when the named field is absent from the primary account.
generate_bearer_config() {
	local field="$1"
	local out
	out="$(mktemp)"
	chmod 600 "${out}"
	if ! python3 - "${ACCOUNTS_FILE}" "${field}" "${_INI_SECTIONS[@]}" >"${out}" <<-'PY'
		import json
		import sys

		manifest_path, field = sys.argv[1], sys.argv[2]
		with open(manifest_path, encoding="utf-8") as f:
		    manifest = json.load(f)
		accounts = {a["role"]: a for a in manifest.get("accounts", [])}
		primary = accounts.get("primary")
		token = primary.get(field) if primary else None
		if not token:
		    sys.exit(f"the primary account in {manifest_path} has no {field}")
		for section in sys.argv[3:]:
		    print(f"[{section}]")
		    if section == "Request":
		        print("authtype = Bearer")
		        print(f"authcred = {token}")
		PY
	then
		rm -f "${out}"
		return 1
	fi
	printf '%s' "${out}"
}

# Signs in as the primary account through a real browser - reusing
# tests/integration/setup/auth.setup.ts exactly as run_nuclei_scan.sh's
# mint_session_secret_file does, rather than reimplementing Django's
# CSRF-protected login form - and turns the resulting cookies into a config
# file carrying them, again kept off argv. Needs Node; prints the generated
# path on success.
mint_session_config() {
	if ! command -v npm >/dev/null 2>&1; then
		echo "no npm on PATH - the session tier needs Node to drive a real login" >&2
		return 1
	fi
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
	if ! python3 - "${state_file}" "${_INI_SECTIONS[@]}" >"${out}" <<-'PY'
		import json
		import sys

		state_path = sys.argv[1]
		with open(state_path, encoding="utf-8") as f:
		    state = json.load(f)
		wanted = {"sessionid", "csrftoken"}
		cookies = [c for c in state.get("cookies", []) if c["name"] in wanted]
		if not any(c["name"] == "sessionid" for c in cookies):
		    sys.exit(f"no sessionid cookie in {state_path}")
		cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
		for section in sys.argv[2:]:
		    print(f"[{section}]")
		    if section == "Request":
		        print(f"cookie = {cookie_header}")
		PY
	then
		rm -f "${out}"
		return 1
	fi
	printf '%s' "${out}"
}

# Reads report.json's {"success", "data", "error"} shape (lib/utils/api.py) -
# sqlmap's own exit code stays 0 on a clean run that found nothing, so this is
# the only reliable finding signal. A TARGET/TECHNIQUES entry in `data` is
# sqlmap confirming it identified an actual injection point, as opposed to
# fingerprint/banner/enumeration entries that describe an already-confirmed
# target further.
count_findings() {
	local report_json="$1"
	if [[ ! -f "${report_json}" ]]; then
		echo 0
		return
	fi
	python3 - "${report_json}" <<-'PY'
		import json
		import sys

		with open(sys.argv[1], encoding="utf-8") as f:
		    report = json.load(f)
		findings = [d for d in report.get("data", []) if d.get("type_name") in ("TARGET", "TECHNIQUES")]
		print(len(findings))
		PY
}

TIER_COUNTS=()
TOTAL_FINDINGS=0

# One openapi-driven run against the published external API. `config_file`
# (from generate_bearer_config, or empty for unauthenticated) is passed via
# -c so a live key never reaches argv.
run_api_tier() {
	local tier_name="$1" config_file="$2" tier_out_dir="$3"
	mkdir -p "${tier_out_dir}"
	local report_json="${tier_out_dir}/report.json"
	rm -f "${report_json}"

	echo ""
	echo "=== ${tier_name} ==="

	local config_args=()
	[[ -n "${config_file}" ]] && config_args=(-c "${config_file}")

	# --ignore-stdin: without it, sqlmap treats this wrapper's redirected/closed
	# stdin as a piped target list (lib/parse/cmdline.py's stdinPipe detection
	# fires on any non-tty stdin, --openapi or not) and _setStdinPipeTargets()
	# unconditionally overwrites kb.targets with its own lazy reader before
	# _setOpenApiTargets() ever runs - confirmed against a real 1.10.8 install,
	# where every --openapi run crashed with "TypeError: object of type '_' has
	# no len()" and never even reached --report-json. Silent-looking too: the
	# crash is loud on stderr, but count_findings()'s own "no report.json yet"
	# fallback reads as a clean 0-finding scan unless the log is actually read.
	"${SQLMAP_BIN}" ${config_args[@]+"${config_args[@]}"} \
		--openapi="${SCHEMA_URL}" \
		--openapi-base="${BASE_URL}" \
		--batch --ignore-stdin \
		--risk="${RISK}" --level="${LEVEL}" --technique="${TECHNIQUE}" \
		--threads="${THREADS}" --delay="${DELAY}" \
		--output-dir="${tier_out_dir}" \
		--report-json="${report_json}" \
		${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
		</dev/null

	local count
	count=$(count_findings "${report_json}")
	echo "${tier_name}: ${count} finding(s)"
	TIER_COUNTS+=("${tier_name}=${count}")
	TOTAL_FINDINGS=$((TOTAL_FINDINGS + count))
}

# The session tier reaches genuinely different ground than the API-key tiers:
# ExternalApiView declares authentication_classes=[ApiKeyAuthentication,
# OAuth2Authentication] with no SessionAuthentication, so a cookie cannot
# reach /api/... at all (see docs/INTEGRATION_TESTS.md) - conversely the
# HTML/HTMX surface only recognises a session. --openapi has nothing to offer
# here, so this crawls instead.
run_session_tier() {
	local config_file="$1"
	local tier_out_dir="${OUT_DIR}/session"
	mkdir -p "${tier_out_dir}"
	local report_json="${tier_out_dir}/report.json"
	rm -f "${report_json}"

	echo ""
	echo "=== session ==="

	# --ignore-stdin isn't load-bearing here the way it is in run_api_tier - -u
	# sets conf.url, which already short-circuits sqlmap's stdin-target
	# detection - but it costs nothing to keep every invocation in this script
	# identically defended against it.
	#
	# --threads is hardcoded to 1 rather than following $THREADS: sqlmap
	# refuses outright ("option '--csrf-token' is incompatible with option
	# '--threads'") whenever --csrf-token is combined with --threads greater
	# than 1 (lib/core/option.py: `if conf.csrfToken and conf.threads > 1`),
	# confirmed against a real 1.10.8 run - re-fetching a token+cookie pair per
	# request isn't safe to parallelise anyway.
	"${SQLMAP_BIN}" -c "${config_file}" \
		-u "${BASE_URL}/" \
		--crawl="${CRAWL_DEPTH}" --crawl-exclude="${CRAWL_EXCLUDE}" --forms \
		--csrf-token=csrfmiddlewaretoken \
		--batch --ignore-stdin \
		--risk="${RISK}" --level="${LEVEL}" --technique="${TECHNIQUE}" \
		--threads=1 --delay="${DELAY}" \
		--output-dir="${tier_out_dir}" \
		--report-json="${report_json}" \
		${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
		</dev/null

	local count
	count=$(count_findings "${report_json}")
	echo "session: ${count} finding(s)"
	TIER_COUNTS+=("session=${count}")
	TOTAL_FINDINGS=$((TOTAL_FINDINGS + count))
}

echo "Scanning ${BASE_URL} (risk=${RISK} level=${LEVEL} technique=${TECHNIQUE})"

if [[ ${ALL_TIERS} -eq 1 ]]; then
	run_api_tier "unauthenticated" "" "${OUT_DIR}/unauthenticated"

	if config_file=$(generate_bearer_config restricted_api_key); then
		CLEANUP_PATHS+=("${config_file}")
		run_api_tier "apikey-restricted" "${config_file}" "${OUT_DIR}/apikey-restricted"
	else
		echo "skipping apikey-restricted" >&2
	fi

	if config_file=$(generate_bearer_config api_key); then
		CLEANUP_PATHS+=("${config_file}")
		run_api_tier "apikey-full" "${config_file}" "${OUT_DIR}/apikey-full"
	else
		echo "skipping apikey-full" >&2
	fi

	if config_file=$(mint_session_config); then
		CLEANUP_PATHS+=("${config_file}")
		run_session_tier "${config_file}"
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
		echo "error: --fail-on-findings set and ${TOTAL_FINDINGS} finding(s) were confirmed across all tiers." >&2
		exit 1
	fi
	exit 0
fi

# Single-tier path (the default): flat reports/sqlmap/ location, at most one
# credential.
SINGLE_CONFIG=""
if [[ -n "${ACCOUNTS_FILE}" ]]; then
	SINGLE_CONFIG="$(generate_bearer_config api_key)"
	CLEANUP_PATHS+=("${SINGLE_CONFIG}")
	echo "Authenticating as the primary account from ${ACCOUNTS_FILE} (Bearer, full scope)"
fi

REPORT_JSON="${OUT_DIR}/report.json"
mkdir -p "${OUT_DIR}"
rm -f "${REPORT_JSON}"

CONFIG_ARGS=()
[[ -n "${SINGLE_CONFIG}" ]] && CONFIG_ARGS=(-c "${SINGLE_CONFIG}")

"${SQLMAP_BIN}" ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"} \
	--openapi="${SCHEMA_URL}" \
	--openapi-base="${BASE_URL}" \
	--batch --ignore-stdin \
	--risk="${RISK}" --level="${LEVEL}" --technique="${TECHNIQUE}" \
	--threads="${THREADS}" --delay="${DELAY}" \
	--output-dir="${OUT_DIR}" \
	--report-json="${REPORT_JSON}" \
	${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
	</dev/null

FINDING_COUNT=$(count_findings "${REPORT_JSON}")
echo ""
echo "${FINDING_COUNT} finding(s). Report: ${REPORT_JSON}"

if [[ ${FINDING_COUNT} -gt 0 && ${FAIL_ON_FINDINGS} -eq 1 ]]; then
	echo "error: --fail-on-findings set and ${FINDING_COUNT} finding(s) were confirmed." >&2
	exit 1
fi
