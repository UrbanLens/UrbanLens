# shellcheck shell=bash
#
# The container names docker-compose.yml gives a stack.
#
# Sourced by the backup, test and sync scripts. Compose names each service's container
# urbanlens_${UL_CONTAINER_NAME:-${UL_ENVIRONMENT:-production}}_<service>, taking the variables from the shell and then
# from .env in the project directory, so this reads them the same way rather than assuming a stack name.
#
# Callers set `set -euo pipefail` themselves; this file deliberately does not.

# The value of NAME in the shell, else in <repo root>/.env (last assignment wins, one pair of quotes removed).
#   $1  the repository root
#   $2  the variable name
ul_env_value() {
    local root=$1 name=$2 value
    if [ -n "${!name:-}" ]; then
        printf '%s\n' "${!name}"
        return
    fi
    [ -f "$root/.env" ] || return 0
    value=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$root/.env" | tail -n 1 | sed -E "s/^[^=]*=//; s/^[[:space:]]+//; s/[[:space:]]+\$//; s/^\"(.*)\"\$/\1/; s/^'(.*)'\$/\1/") || true
    printf '%s\n' "$value"
}

# The container compose creates for a service: urbanlens_<stack>_<suffix>, where the suffix is the service name with
# dashes as underscores (test-runner is test_runner).
#   $1  the repository root
#   $2  the container suffix, e.g. app, db, test_runner
ul_container_name() {
    local root=$1 suffix=$2 stack
    stack=$(ul_env_value "$root" UL_CONTAINER_NAME)
    [ -n "$stack" ] || stack=$(ul_env_value "$root" UL_ENVIRONMENT)
    printf 'urbanlens_%s_%s\n' "${stack:-production}" "$suffix"
}

# The app container's name: UL_APP_CONTAINER if set, else what compose would create.
#   $1  the repository root
ul_app_container_name() {
    local root=$1
    if [ -n "${UL_APP_CONTAINER:-}" ]; then
        printf '%s\n' "$UL_APP_CONTAINER"
        return
    fi
    ul_container_name "$root" app
}
