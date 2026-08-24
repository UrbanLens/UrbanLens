#!/usr/bin/env bash
#
# Run the integration suite against a deployed instance.
#
# The suite itself is `tests/integration/` (Playwright). This wrapper exists for
# the three things that are easy to get wrong by hand and expensive to discover
# thirty seconds into a run:
#
#   - the browser build has to match the @playwright/test version exactly, or
#     the run fails with a version-mismatch error that reads like a bug;
#   - the target has to be stated explicitly, because the default for a suite
#     that writes and deletes data must never be "whatever was set last";
#   - the accounts have to be provisioned on the deployment first, since sign-up
#     alone leaves an account inactive pending an emailed link nothing here can
#     click.
#
# Usage:
#   bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org
#   bin/run_integration_tests.sh --url ... --project smoke
#   bin/run_integration_tests.sh --url ... --docker         # no local Node needed
#   bin/run_integration_tests.sh --url ... -- --grep "@slow" # pass through
#
# Anything after `--` is handed to `playwright test` unchanged.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUITE_DIR="${REPO_ROOT}/tests/integration"

BASE_URL="${UL_E2E_BASE_URL:-}"
PROJECTS=()
USE_DOCKER=0
INSTALL_BROWSERS=1
PASSTHROUGH=()

usage() {
	cat <<-EOF
		Run the integration suite against a deployed instance.

		  --url URL                 The deployment to test (or set UL_E2E_BASE_URL).
		  --project NAME            Restrict to one project; repeatable.
		                            smoke | services | api | ui | a11y | visual
		  --docker                  Run in the official Playwright image; needs no
		                            local Node or browsers.
		  --skip-browser-install    Do not check for a matching browser build.
		  -- ARGS...                Everything after this goes to \`playwright test\`.

		Accounts come from UL_E2E_ACCOUNTS_FILE, written on the deployment by
		\`manage.py provision_integration_env\`. See tests/integration/.env.example.
	EOF
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--url)
			BASE_URL="$2"
			shift 2
			;;
		--project)
			PROJECTS+=("--project=$2")
			shift 2
			;;
		--docker)
			USE_DOCKER=1
			shift
			;;
		--skip-browser-install)
			INSTALL_BROWSERS=0
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

# node_modules lives in the (gitignored) suite directory and is mounted into the
# container, so it survives between runs. `npm ci` would delete and rebuild it
# every time, which is minutes per run for no benefit.
install_command() {
	if [[ -d "${SUITE_DIR}/node_modules" ]]; then
		echo "true"
	elif [[ -f "${SUITE_DIR}/package-lock.json" ]]; then
		echo "npm ci --no-audit --no-fund"
	else
		echo "npm install --no-audit --no-fund"
	fi
}

if [[ -z "${BASE_URL}" ]]; then
	echo "error: no target. Pass --url https://your-staging-host, or set UL_E2E_BASE_URL." >&2
	exit 2
fi

if [[ -z "${UL_E2E_ACCOUNTS_FILE:-}" && -z "${UL_E2E_USERNAME:-}" && ! -f "${SUITE_DIR}/.env" ]]; then
	cat >&2 <<-EOF
		error: no accounts configured.

		On the deployment under test, run:
		  python src/urbanlens/manage.py provision_integration_env --out /tmp/e2e.json

		then point this at the manifest it wrote:
		  UL_E2E_ACCOUNTS_FILE=/tmp/e2e.json $0 --url ${BASE_URL}

		See tests/integration/.env.example for the single-account alternative.
	EOF
	exit 2
fi

# The image tag and the installed package must be the same version, so both are
# read from the one place that pins it.
PLAYWRIGHT_VERSION="$(grep -o '"@playwright/test": *"[^"]*"' "${SUITE_DIR}/package.json" | grep -o '[0-9][0-9.]*')"
if [[ -z "${PLAYWRIGHT_VERSION}" ]]; then
	echo "error: could not read the pinned @playwright/test version from ${SUITE_DIR}/package.json" >&2
	exit 1
fi

export UL_E2E_BASE_URL="${BASE_URL}"

if [[ ${USE_DOCKER} -eq 1 ]]; then
	# The official image carries a matching Node and matching browsers, so this
	# is the path that needs nothing installed on the machine running it.
	IMAGE="mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble"
	echo "Running in ${IMAGE} against ${BASE_URL}"

	# Interactive only when there is a terminal to be interactive with; `-it`
	# against a pipe (a CI step, an `ssh host '...'`) fails outright.
	TTY_FLAGS=()
	if [[ -t 0 && -t 1 ]]; then
		TTY_FLAGS=(-it)
	fi

	# `--ipc=host` because Chromium's default 64MB shared-memory allocation in a
	# container makes tabs crash on large pages; `--network=host` so a target on
	# this machine (or reachable only from it) resolves the same way it does here.
	exec docker run --rm ${TTY_FLAGS[@]+"${TTY_FLAGS[@]}"} \
		--ipc=host \
		--network=host \
		-v "${SUITE_DIR}:/suite" \
		${UL_E2E_ACCOUNTS_FILE:+-v "${UL_E2E_ACCOUNTS_FILE}:${UL_E2E_ACCOUNTS_FILE}:ro"} \
		-w /suite \
		-e UL_E2E_BASE_URL \
		-e UL_E2E_ACCOUNTS_FILE \
		-e UL_E2E_USERNAME -e UL_E2E_PASSWORD -e UL_E2E_API_KEY -e UL_E2E_SCOPES \
		-e UL_E2E_RESTRICTED_API_KEY -e UL_E2E_RESTRICTED_SCOPES \
		-e UL_E2E_SECONDARY_USERNAME -e UL_E2E_SECONDARY_PASSWORD -e UL_E2E_SECONDARY_API_KEY \
		-e UL_E2E_IGNORE_HTTPS_ERRORS -e UL_E2E_EXPECT_PRIMARY -e UL_E2E_REDATA_URL \
		-e UL_E2E_WORKERS -e UL_E2E_RETRIES -e UL_E2E_RUN_ID -e UL_E2E_STRICT_CONSOLE \
		-e UL_E2E_CROSS_BROWSER -e UL_E2E_VISUAL -e UL_E2E_WS_IDLE_SECONDS \
		-e CI \
		"${IMAGE}" \
		bash -lc "$(install_command) && npx playwright test ${PROJECTS[*]:-} ${PASSTHROUGH[*]:-}"
fi

command -v npm >/dev/null 2>&1 || {
	echo "error: npm is not on PATH. Install Node 20+, or re-run with --docker." >&2
	exit 1
}

cd "${SUITE_DIR}"

if [[ ! -d node_modules ]]; then
	echo "Installing suite dependencies..."
	eval "$(install_command)"
fi

if [[ ${INSTALL_BROWSERS} -eq 1 ]]; then
	# Cheap when already present: this verifies the browser build matching the
	# installed package is there and downloads it only if it is not.
	npx playwright install chromium
	if [[ "${UL_E2E_CROSS_BROWSER:-0}" != "0" ]]; then
		npx playwright install firefox webkit
	fi
fi

echo "Running the integration suite against ${BASE_URL}"
# The `${arr[@]+...}` form rather than a bare `"${arr[@]}"`: under `set -u`, an
# empty array expansion is an unbound-variable error on bash 3.2 (macOS).
npx playwright test ${PROJECTS[@]+"${PROJECTS[@]}"} ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
