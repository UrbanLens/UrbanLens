#!/usr/bin/env bash
#
# Run the integration suite against a deployed instance.
#
# Wrapper pins the matching browser build, requires an explicit target, and needs provisioned accounts.
#
# Usage:
#   bin/run_integration_tests.sh --url https://s1.dev.urbanlens.org
#   bin/run_integration_tests.sh --url ... --project smoke
#   bin/run_integration_tests.sh --url ... --project location   # sets UL_E2E_LOCATION_DATA=1
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
		                            smoke | services | api | ui | a11y | security |
		                            location | visual | ui-firefox | ui-webkit | ui-mobile
		                            The last five are opt-in; naming one sets its
		                            UL_E2E_LOCATION_DATA / UL_E2E_VISUAL / UL_E2E_CROSS_BROWSER.
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
			case "$2" in
				location) export UL_E2E_LOCATION_DATA="${UL_E2E_LOCATION_DATA:-1}" ;;
				visual) export UL_E2E_VISUAL="${UL_E2E_VISUAL:-1}" ;;
				ui-firefox | ui-webkit | ui-mobile) export UL_E2E_CROSS_BROWSER="${UL_E2E_CROSS_BROWSER:-1}" ;;
			esac
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

# node_modules is mounted into the container and reused; `npm ci` every run would cost minutes.
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

# Image tag and package must match; both read from the pinned version.
PLAYWRIGHT_VERSION="$(grep -o '"@playwright/test": *"[^"]*"' "${SUITE_DIR}/package.json" | grep -o '[0-9][0-9.]*')"
if [[ -z "${PLAYWRIGHT_VERSION}" ]]; then
	echo "error: could not read the pinned @playwright/test version from ${SUITE_DIR}/package.json" >&2
	exit 1
fi

export UL_E2E_BASE_URL="${BASE_URL}"
# The Docker runner mounts only the suite directory, so git cannot be asked from inside it.
UL_E2E_GIT_SHA="${UL_E2E_GIT_SHA:-$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || true)}"
export UL_E2E_GIT_SHA

if [[ ${USE_DOCKER} -eq 1 ]]; then
	# Official image already carries matching Node and browsers.
	IMAGE="mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble"
	echo "Running in ${IMAGE} against ${BASE_URL}"

	# `-it` only with a terminal; against a pipe it fails.
	TTY_FLAGS=()
	if [[ -t 0 && -t 1 ]]; then
		TTY_FLAGS=(-it)
	fi

	# Every exported UL_E2E_* variable, so a new one needs no edit here.
	ENV_FLAGS=(-e CI)
	while IFS= read -r name; do
		ENV_FLAGS+=(-e "${name}")
	done < <(compgen -e | grep '^UL_E2E_' || true)

	# Larger shm so Chromium tabs survive large pages; host networking so local targets resolve.
	exec docker run --rm ${TTY_FLAGS[@]+"${TTY_FLAGS[@]}"} \
		--ipc=host \
		--network=host \
		-v "${SUITE_DIR}:/suite" \
		${UL_E2E_ACCOUNTS_FILE:+-v "${UL_E2E_ACCOUNTS_FILE}:${UL_E2E_ACCOUNTS_FILE}:ro"} \
		-w /suite \
		"${ENV_FLAGS[@]}" \
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
	# No-op when the matching build is already present.
	npx playwright install chromium
	if [[ "${UL_E2E_CROSS_BROWSER:-0}" != "0" ]]; then
		npx playwright install firefox webkit
	fi
fi

echo "Running the integration suite against ${BASE_URL}"
# `${arr[@]+...}` guards empty arrays under `set -u` on bash 3.2.
npx playwright test ${PROJECTS[@]+"${PROJECTS[@]}"} ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
