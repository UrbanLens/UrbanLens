#!/bin/bash
# Clones the production PostGIS database into the staging environment.
#
# Assumes prod and staging are separate checkouts of this repo on the same
# Docker host, each with its own docker-compose.yml/.env, and that this script
# is run from inside the staging checkout (it drives staging's `docker compose`
# using the compose file in the current directory). Only prod's .env is read
# remotely, via --prod-dir, to find its container name and DB credentials -
# nothing is executed inside the prod checkout itself.
#
# Run on the Docker host (Ubuntu), not from a Windows dev machine - it shells
# out to `docker` / `docker compose` directly.
#
# Usage (from inside the staging checkout):
#   ./bin/clone_prod_to_staging.sh --prod-dir /path/to/prod-checkout [options]
#   UL_PROD_DIR=/path/to/prod-checkout ./bin/clone_prod_to_staging.sh [options]
#   # or set UL_PROD_DIR=/path/to/prod-checkout in the staging checkout's .env
#
#   --prod-dir DIR          Path to the production checkout. Falls back to the
#                           UL_PROD_DIR shell variable, then to UL_PROD_DIR in
#                           the staging .env, in that order of precedence.
#   --prod-env-file NAME    Env filename inside --prod-dir (default: .env)
#   --staging-env-file NAME Env filename in the current directory (default: .env)
#   -y, --yes               Skip the confirmation prompt
#   --keep-dump             Don't delete the local dump file after restoring
set -euo pipefail

PROD_DIR="${UL_PROD_DIR:-}"
PROD_ENV_FILENAME=".env"
STAGING_ENV_FILENAME=".env"
ASSUME_YES=false
KEEP_DUMP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prod-dir) PROD_DIR="$2"; shift 2 ;;
        --prod-env-file) PROD_ENV_FILENAME="$2"; shift 2 ;;
        --staging-env-file) STAGING_ENV_FILENAME="$2"; shift 2 ;;
        -y|--yes) ASSUME_YES=true; shift ;;
        --keep-dump) KEEP_DUMP=true; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ ! -f "./docker-compose.yml" ]]; then
    echo "No docker-compose.yml in the current directory - run this from inside the staging checkout." >&2
    exit 1
fi

STAGING_ENV_FILE="./$STAGING_ENV_FILENAME"

if [[ ! -f "$STAGING_ENV_FILE" ]]; then
    echo "Env file not found: $STAGING_ENV_FILE" >&2
    exit 1
fi

# Read a KEY from an env file without polluting the current shell's environment
# (the files carry API keys/secrets we have no reason to export here).
read_env_var() {
    local file="$1" key="$2" default="${3:-}"
    local line
    line=$(grep -E "^${key}=" "$file" | tail -n1 || true)
    if [[ -z "$line" ]]; then
        echo "$default"
    else
        echo "${line#*=}"
    fi
}

# Fall back to UL_PROD_DIR documented in the staging .env itself if neither
# --prod-dir nor a shell-level UL_PROD_DIR were provided.
if [[ -z "$PROD_DIR" ]]; then
    PROD_DIR=$(read_env_var "$STAGING_ENV_FILE" UL_PROD_DIR "")
fi

if [[ -z "$PROD_DIR" ]]; then
    echo "Path to the production checkout is required: pass --prod-dir, set the UL_PROD_DIR shell variable, or set UL_PROD_DIR in $STAGING_ENV_FILE." >&2
    exit 1
fi

PROD_ENV_FILE="${PROD_DIR%/}/$PROD_ENV_FILENAME"

if [[ ! -f "$PROD_ENV_FILE" ]]; then
    echo "Env file not found: $PROD_ENV_FILE" >&2
    exit 1
fi

PROD_ENVIRONMENT=$(read_env_var "$PROD_ENV_FILE" UL_ENVIRONMENT production)
STAGING_ENVIRONMENT=$(read_env_var "$STAGING_ENV_FILE" UL_ENVIRONMENT staging)

if [[ "$STAGING_ENVIRONMENT" == "$PROD_ENVIRONMENT" ]]; then
    echo "Refusing to run: the staging checkout resolves to UL_ENVIRONMENT=$STAGING_ENVIRONMENT, same as --prod-dir." >&2
    echo "This script's restore step wipes the target database - pointing it at production would destroy live data." >&2
    exit 1
fi

if [[ "$STAGING_ENVIRONMENT" == "production" ]]; then
    echo "Refusing to run: the staging checkout's own .env resolves to UL_ENVIRONMENT=production." >&2
    exit 1
fi

PROD_DB_CONTAINER="urbanlens_${PROD_ENVIRONMENT}_db"
STAGING_DB_CONTAINER="urbanlens_${STAGING_ENVIRONMENT}_db"

PROD_DB_USER=$(read_env_var "$PROD_ENV_FILE" UL_DB_USER postgres)
PROD_DB_NAME=$(read_env_var "$PROD_ENV_FILE" UL_DB_NAME postgres)
PROD_DB_PASS=$(read_env_var "$PROD_ENV_FILE" UL_DB_PASS postgres)

STAGING_DB_USER=$(read_env_var "$STAGING_ENV_FILE" UL_DB_USER postgres)
STAGING_DB_NAME=$(read_env_var "$STAGING_ENV_FILE" UL_DB_NAME postgres)
STAGING_DB_PASS=$(read_env_var "$STAGING_ENV_FILE" UL_DB_PASS postgres)

TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)
DUMP_FILE="prod_to_staging_${TIMESTAMP}.dump"

echo "Production:  dir=$PROD_DIR container=$PROD_DB_CONTAINER db=$PROD_DB_NAME user=$PROD_DB_USER"
echo "Staging:     dir=$(pwd) container=$STAGING_DB_CONTAINER db=$STAGING_DB_NAME user=$STAGING_DB_USER"
echo "Dump file:   ./$DUMP_FILE"
echo

if ! $ASSUME_YES; then
    read -r -p "This will DROP AND REPLACE all data in the staging database. Continue? [y/N] " reply
    if [[ ! "$reply" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
fi

wait_for_healthy() {
    local container="$1" tries=30
    while (( tries > 0 )); do
        local status
        status=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "starting")
        if [[ "$status" == "healthy" ]]; then
            return 0
        fi
        sleep 2
        (( tries-- ))
    done
    echo "Timed out waiting for $container to become healthy." >&2
    exit 1
}

echo "==> Dumping production database..."
docker exec -e PGPASSWORD="$PROD_DB_PASS" "$PROD_DB_CONTAINER" \
    pg_dump -U "$PROD_DB_USER" -d "$PROD_DB_NAME" -Fc -f /tmp/clone.dump
docker cp "$PROD_DB_CONTAINER:/tmp/clone.dump" "./$DUMP_FILE"
docker exec "$PROD_DB_CONTAINER" rm -f /tmp/clone.dump

echo "==> Bringing up the staging db/valkey services..."
docker compose --env-file "$STAGING_ENV_FILE" up -d db valkey
wait_for_healthy "$STAGING_DB_CONTAINER"

echo "==> Restoring into staging..."
docker cp "./$DUMP_FILE" "$STAGING_DB_CONTAINER:/tmp/clone.dump"
docker exec -e PGPASSWORD="$STAGING_DB_PASS" "$STAGING_DB_CONTAINER" \
    pg_restore -U "$STAGING_DB_USER" -d "$STAGING_DB_NAME" --no-owner --clean --if-exists -j4 /tmp/clone.dump
docker exec "$STAGING_DB_CONTAINER" rm -f /tmp/clone.dump

if ! $KEEP_DUMP; then
    rm -f "./$DUMP_FILE"
fi

echo "==> Bringing up the rest of the staging stack..."
docker compose --env-file "$STAGING_ENV_FILE" up -d --build

echo "Done. Staging now mirrors production as of $TIMESTAMP."
