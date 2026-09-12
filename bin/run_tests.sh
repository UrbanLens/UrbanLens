#!/usr/bin/env bash
#
# Run pytest inside the test container, with the sync this repo requires.
#
# Syncs all of src/ (init.py is real imported code, not just the package).
#
# /app/src is baked into the image, so copy in and verify parity rather than assuming it.
#
# Usage:
#   bin/run_tests.sh [pytest args...]           # sync, then run
#   bin/run_tests.sh --no-sync [pytest args...] # reuse the container as-is
#   bin/run_tests.sh --verify-only              # just compare host and container
#   bin/run_tests.sh --allow-drift ...          # run despite drift, on purpose
#   bin/run_tests.sh --fast [pytest args...]    # reuse a persistent database
#   bin/run_tests.sh --fresh-db [pytest args...]# rebuild it (needed after a migration)
#   bin/run_tests.sh --force --fresh-db ...     # rebuild even if something is connected
#   bin/run_tests.sh --parallel[=N] [args...]   # N xdist workers (default: auto)
#   bin/run_tests.sh --shuffle [pytest args...] # randomise test order
#   bin/run_tests.sh --no-venv-fix ...          # do not install missing dev deps
#
# --fast reuses a persistent DB (rebuild after migrations); set UL_TEST_DB_NAME per session on shared hosts.
#
# --parallel gives each xdist worker its own DB; wins on large selections, loses on single files.
#
# --shuffle is opt-in; reproduce failures with the printed seed.
#
# Environment:
#   UL_TEST_CONTAINER   test-runner container name (default urbanlens_development_main_test_runner)
#   UL_TEST_DB_NAME     test database name. Without --fast/--fresh-db, a unique one is
#                       generated when unset (parallel runs collide otherwise, and the
#                       channel-layer prefix derives from it). With --fast/--fresh-db the
#                       default is the fixed name 'ul_fast' for reuse - so on a shared host
#                       always set this explicitly.
set -euo pipefail

CONTAINER="${UL_TEST_CONTAINER:-urbanlens_development_main_test_runner}"
VENV_FIX=1
SYNC=1
VERIFY_ONLY=0
FAST=0
FRESH_DB=0
FORCE_DROP=0
# Deliberate container drift needs a way past the guard; named to avoid accidents.
ALLOW_DRIFT=0
PARALLEL=""
SHUFFLE=0

# The copy-into-a-container sequence is shared with bin/sync_app.sh.
# shellcheck source=bin/lib/container_sync.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib/container_sync.sh"

args=()
for arg in "$@"; do
    case "$arg" in
        --no-sync) SYNC=0 ;;
        --allow-drift) ALLOW_DRIFT=1 ;;
        --fast) FAST=1 ;;
        --fresh-db) FAST=1; FRESH_DB=1 ;;
        --force) FORCE_DROP=1 ;;
        --verify-only) VERIFY_ONLY=1 ;;
        --parallel) PARALLEL="auto" ;;
        --parallel=*) PARALLEL="${arg#*=}" ;;
        --shuffle) SHUFFLE=1 ;;
        --no-venv-fix) VENV_FIX=0 ;;
        *) args+=("$arg") ;;
    esac
done

if [ -n "$PARALLEL" ]; then
    # --dist loadfile keeps each file's tests on one worker. Several suites here
    # build expensive per-class state, and splitting a class across workers pays
    # that cost once per worker instead of once.
    args=(-n "$PARALLEL" --dist loadfile "${args[@]}")
fi
if [ "$SHUFFLE" -eq 1 ]; then
    # Undoes the `-p no:randomly` in pyproject's addopts; a later -p wins.
    args=(-p randomly "${args[@]}")
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "error: container '$CONTAINER' not found. Set UL_TEST_CONTAINER or start the stack." >&2
    exit 2
fi

sync_tree() { sync_tree_into "$CONTAINER"; }
verify_parity() { verify_parity_with "$CONTAINER"; }

verify_frontend_build() {
    # The compiled bundles are not in git, so a host that never built hands the
    # container bundles that do not match the templates. Warn rather than fail
    # mysteriously in test_compiled_js_references_resolve.py.
    local js_dir="src/urbanlens/dashboard/frontend/static/dashboard/js"
    local ts_dir="src/urbanlens/dashboard/frontend/ts"
    [ -d "$ts_dir" ] || return 0

    local newest
    newest=$(find "$js_dir" -name '*.js' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    if [ -z "$newest" ]; then
        echo "warning: no compiled JS bundles in $js_dir - the frontend has never been built here." >&2
        echo "    test_compiled_js_references_resolve.py skips rather than passing vacuously. Build: bun run build" >&2
        return 0
    fi

    # Test sources aren't bundle inputs; a newer one is not staleness.
    if [ -n "$(find "$ts_dir" \( -name '*.ts' -o -name '*.tsx' \) -not -name '*.test.ts' -not -name '*.spec.ts' -newer "$newest" -print -quit 2>/dev/null)" ]; then
        echo "warning: TypeScript sources are newer than the compiled bundles being synced." >&2
        echo "    A template naming a new entry point fails as a missing bundle. Rebuild: bun run build" >&2
    fi
}

verify_venv() {
    # Deps aren't synced, so a post-build addition surfaces as a collection error. Checked against uv's full install set.
    local missing
    # -i, or the heredoc never reaches the interpreter.
    missing=$(docker exec -i "$CONTAINER" /app/.venv/bin/python - <<'PY' 2>/dev/null
import re
import tomllib
from importlib.metadata import PackageNotFoundError, version

with open("/app/pyproject.toml", "rb") as handle:
    config = tomllib.load(handle)

specs = list(config.get("project", {}).get("dependencies", []))
specs += config.get("dependency-groups", {}).get("dev", [])

for spec in specs:
    name = re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].strip()
    if not name:
        continue
    try:
        version(name)
    except PackageNotFoundError:
        print(name)
PY
)
    [ -n "$missing" ] || return 0

    echo "==> the container's venv predates these dependencies:" >&2
    echo "$missing" | sed 's|^|      |' >&2

    if [ "$VENV_FIX" -eq 0 ]; then
        echo "    --no-venv-fix: a test importing one will fail at collection, naming the module" >&2
        echo "    rather than the cause. Rebuild: docker compose --profile test up -d --build test-runner" >&2
        return 0
    fi

    # Installing them beats warning about them: a rebuild is an operator action
    # on a container other sessions may share, so the warning kept not working.
    # Installs exactly what pyproject declares, constraints included.
    echo "==> installing them from pyproject (--no-venv-fix to skip)" >&2
    local specs
    specs=$(docker exec -i "$CONTAINER" /app/.venv/bin/python - "$missing" <<'SPECS' 2>/dev/null
import re
import sys
import tomllib

wanted = set(sys.argv[1].split())
with open("/app/pyproject.toml", "rb") as handle:
    config = tomllib.load(handle)

specs = list(config.get("project", {}).get("dependencies", []))
specs += config.get("dependency-groups", {}).get("dev", [])
for spec in specs:
    if re.split(r"[<>=!~\[; ]", spec, maxsplit=1)[0].strip() in wanted:
        print(spec)
SPECS
)
    if [ -z "$specs" ]; then
        echo "    could not resolve their specs from pyproject.toml; skipping" >&2
        return 0
    fi
    # shellcheck disable=SC2086
    if ! docker exec -e VIRTUAL_ENV=/app/.venv "$CONTAINER" /app/.venv/bin/uv pip install --quiet $specs >&2; then
        echo "    install failed - the run continues, and a test importing one of these will fail" >&2
        echo "    at collection naming the module rather than the cause." >&2
        echo "    Rebuild: docker compose --profile test up -d --build test-runner" >&2
    fi
}

[ "$SYNC" -eq 1 ] && sync_tree
if [ "$ALLOW_DRIFT" -eq 1 ]; then
    echo "==> skipping parity check (--allow-drift): the container is expected to differ"
else
    verify_parity
fi
verify_venv
verify_frontend_build
[ "$VERIFY_ONLY" -eq 1 ] && exit 0

if [ "$FAST" -eq 1 ]; then
    # A stable name, so the database survives between runs and can be reused.
    DB_NAME="${UL_TEST_DB_NAME:-ul_fast}"
    if [ "$FRESH_DB" -eq 1 ]; then
        DB_FLAG="--create-db"
        echo "==> rebuilding the reusable database '$DB_NAME'"
        # Terminate and drop first so "fresh" is actually fresh; -i or the heredoc is a silent no-op.
        # Refuses with live connections (likely another session); --force overrides for your own abandoned run.
        docker exec -i -e DJANGO_SETTINGS_MODULE=urbanlens.UrbanLens.settings.test "$CONTAINER" /app/.venv/bin/python - "$DB_NAME" "$FORCE_DROP" <<'DROP_DB'
import sys

import django

django.setup()
from django.db import connection

name = sys.argv[1]
force = sys.argv[2] == "1"
params = connection.get_connection_params()
# psycopg2 spells it "dbname"; Django's params carry the test database, and
# a session cannot drop the database it is connected to.
params.pop("database", None)
params["dbname"] = "postgres"
# Not `with connection.Database.connect(...)`: in psycopg2 that context
# manager opens a *transaction*, and DROP DATABASE cannot run inside one.
maintenance = connection.Database.connect(**params)
try:
    maintenance.autocommit = True
    with maintenance.cursor() as cursor:
        cursor.execute(
            "SELECT pid, state, query_start, left(query, 80) FROM pg_stat_activity WHERE datname = %s",
            [name],
        )
        active = cursor.fetchall()
        if active and not force:
            print(f"error: '{name}' has {len(active)} active connection(s) - refusing to drop it:", file=sys.stderr)
            for pid, state, query_start, query in active:
                print(f"    pid={pid} state={state} since={query_start} query={query!r}", file=sys.stderr)
            print(
                "This is more likely another session's in-progress run than a leftover from an "
                "interrupted one of yours - on a shared host, assume it is someone else's until "
                "proven otherwise. Pick a different UL_TEST_DB_NAME, or pass --force only if you "
                "are certain this is your own abandoned session.",
                file=sys.stderr,
            )
            sys.exit(1)
        cursor.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s", [name])
        cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
finally:
    maintenance.close()
print(f"    dropped '{name}' if it existed", flush=True)
DROP_DB
    else
        DB_FLAG="--reuse-db"
        # --reuse-db does not apply new migrations, so rebuild with --fresh-db
        # when models move.
        echo "==> reusing database '$DB_NAME' (run --fresh-db after any migration)"
    fi
else
    DB_NAME="${UL_TEST_DB_NAME:-t_$(date +%s)_$$}"
    DB_FLAG=""
fi

echo "==> pytest (UL_TEST_DB_NAME=$DB_NAME)"
if [ -n "$DB_FLAG" ]; then
    docker exec -e UL_TEST_DB_NAME="$DB_NAME" "$CONTAINER" /app/.venv/bin/python -m pytest "$DB_FLAG" "${args[@]}"
else
    docker exec -e UL_TEST_DB_NAME="$DB_NAME" "$CONTAINER" /app/.venv/bin/python -m pytest "${args[@]}"
fi
