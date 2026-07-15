#!/bin/bash
# Pulls the latest commit for a branch and rebuilds the docker compose stack
# in the current directory - the scripted equivalent of manually running
# `docker compose down && git pull && docker compose up --build` over ssh.
#
# Invoked by bin/deploy_webhook.py in response to a verified push webhook, but
# safe to run by hand too. Must be run from inside a checkout that already has
# docker-compose.yml alongside it (same convention as clone_prod_to_staging.sh).
#
# Usage:
#   ./bin/deploy.sh [branch]
#   UL_DEPLOY_BRANCH=staging ./bin/deploy.sh
#
# Branch resolution order: positional arg, then UL_DEPLOY_BRANCH, then whatever
# branch is currently checked out. A lock file prevents two deploys (e.g. a
# retried webhook delivery arriving while a build is still running) from
# racing each other. Refuses to run if the working tree has uncommitted
# changes, or if the checked-out branch doesn't match the target branch - a
# deploy checkout should never carry local edits.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -f "./docker-compose.yml" ]]; then
    echo "No docker-compose.yml found - this script must run from inside the repo checkout." >&2
    exit 1
fi

LOG_FILE="${UL_DEPLOY_LOG_FILE:-./deploy.log}"

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG_FILE"; }

BRANCH="${1:-${UL_DEPLOY_BRANCH:-}}"
if [[ -z "$BRANCH" ]]; then
    BRANCH=$(git rev-parse --abbrev-ref HEAD)
fi

LOCK_FILE="./.deploy.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "Deploy already in progress, skipping this trigger."
    exit 1
fi

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "$BRANCH" ]]; then
    log "Refusing to deploy: checked out branch is '$CURRENT_BRANCH', expected '$BRANCH'."
    exit 1
fi

# --untracked-files=no: only tracked-file modifications are a hazard here -
# `git reset --hard` never touches untracked files, and the deploy log/lock
# files this script itself writes into the checkout are untracked.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    log "Refusing to deploy: working tree has uncommitted changes. Resolve or stash them manually first."
    exit 1
fi

log "==> Fetching origin/$BRANCH..."
git fetch origin "$BRANCH"

LOCAL_SHA=$(git rev-parse HEAD)
REMOTE_SHA=$(git rev-parse "origin/$BRANCH")

if [[ "$LOCAL_SHA" == "$REMOTE_SHA" ]]; then
    log "Already up to date at $LOCAL_SHA - nothing to deploy."
    exit 0
fi

log "==> Updating $LOCAL_SHA -> $REMOTE_SHA"
git reset --hard "origin/$BRANCH"

log "==> docker compose down"
docker compose down

log "==> docker compose up --build -d"
docker compose up --build -d

log "==> Deploy complete at $(git rev-parse --short HEAD)"
