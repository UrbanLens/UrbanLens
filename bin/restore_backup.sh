#!/usr/bin/env bash
#
# Restore one of `core/controllers/backups/db.py`'s dumps into a scratch database.
#
# Dumps are plain SQL; restore from the app container into an empty DB with ON_ERROR_STOP + --single-transaction.
#
# Never writes to the live database - see docs/BACKUPS.md.
#
# Usage:
#   bin/restore_backup.sh --list
#   bin/restore_backup.sh <backup-file> <target-database>
#   bin/restore_backup.sh <backup-file> <target-database> --drop-existing
#   bin/restore_backup.sh <backup-file> <target-database> --dry-run
#
# <backup-file> is either a bare `backup_YYYYMMDD_HHMMSS.sql` (resolved in the
# container's backup directory) or a path to one on this host.
#
# Environment:
#   UL_APP_CONTAINER   app container name (default urbanlens_development_main_app)

set -euo pipefail

CONTAINER="${UL_APP_CONTAINER:-urbanlens_development_main_app}"
BACKUP_DIR=/app/src/backups

die() { echo "error: $*" >&2; exit 1; }

in_app() { docker exec "$@"; }

require_container() {
    docker inspect "$CONTAINER" >/dev/null 2>&1 \
        || die "container '$CONTAINER' not found. Set UL_APP_CONTAINER or start the stack."
    in_app "$CONTAINER" true >/dev/null 2>&1 \
        || die "'$CONTAINER' is not accepting commands (restarting?). Check 'docker logs $CONTAINER'."
}

list_backups() {
    in_app "$CONTAINER" sh -c "ls -la $BACKUP_DIR 2>/dev/null | grep -E 'backup_[0-9]{8}_[0-9]{6}\.sql$'" \
        || die "no backups found in $BACKUP_DIR"
}

DROP_EXISTING=0
DRY_RUN=0
POSITIONAL=()
for arg in "$@"; do
    case "$arg" in
        --list) require_container; list_backups; exit 0 ;;
        --drop-existing) DROP_EXISTING=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) die "unknown option: $arg" ;;
        *) POSITIONAL+=("$arg") ;;
    esac
done

[ "${#POSITIONAL[@]}" -eq 2 ] || die "usage: bin/restore_backup.sh <backup-file> <target-database> [--drop-existing] [--dry-run]"
BACKUP="${POSITIONAL[0]}"
TARGET="${POSITIONAL[1]}"

# Empty string matches neither pattern below, so check it separately.
[ -n "$TARGET" ] || die "target database name is empty"
case "$TARGET" in
    [!a-zA-Z_]*|*[!a-zA-Z0-9_]*) die "target database name must be [A-Za-z_][A-Za-z0-9_]*, got '$TARGET'" ;;
esac

require_container

# Where the file lives: host path is copied in, bare name resolves in the backup dir.
if [ -f "$BACKUP" ]; then
    REMOTE="/tmp/$(basename "$BACKUP")"
    echo "==> copying $BACKUP into $CONTAINER:$REMOTE"
    docker cp "$BACKUP" "$CONTAINER:$REMOTE" >/dev/null
    # Dumps are plaintext; remove the /tmp copy on exit.
    trap 'docker exec -u root "$CONTAINER" rm -f "$REMOTE" 2>/dev/null || true' EXIT
else
    REMOTE="$BACKUP_DIR/$(basename "$BACKUP")"
    in_app "$CONTAINER" test -f "$REMOTE" \
        || die "'$BACKUP' is neither a file on this host nor a name in $BACKUP_DIR. Try --list."
fi

DB_USER=$(in_app "$CONTAINER" printenv UL_DB_USER)
DB_HOST=$(in_app "$CONTAINER" printenv UL_DB_HOST)
DB_PORT=$(in_app "$CONTAINER" printenv UL_DB_PORT)
LIVE_DB=$(in_app "$CONTAINER" printenv UL_DB_NAME)

[ -n "$LIVE_DB" ] \
    || die "'$CONTAINER' has no UL_DB_NAME, so the guard against restoring over the live database cannot be evaluated. Refusing rather than guessing."
[ "$TARGET" != "$LIVE_DB" ] \
    || die "'$TARGET' is the live database this deployment serves. Restore into a scratch database and cut over deliberately - see docs/BACKUPS.md."

# Read the password inside the container so it never appears in the host process table.
# `shift`, not "${@:4}": the container's /bin/sh is dash.
psql_t() {
    # shellcheck disable=SC2016  # the single quotes are the point: $UL_DB_PASS expands in the container, not here
    in_app "$CONTAINER" sh -c 'export PGPASSWORD="$UL_DB_PASS"; u=$1; h=$2; p=$3; shift 3; exec psql -U "$u" -h "$h" -p "$p" "$@"' \
        _ "$DB_USER" "$DB_HOST" "$DB_PORT" "$@"
}

# Dump's CREATE EXTENSION needs a superuser; fail fast rather than mid-restore.
SUPER=$(psql_t -d postgres -tAc "select rolsuper from pg_roles where rolname = current_user;")
[ "$SUPER" = "t" ] \
    || die "role '$DB_USER' is not a superuser, and the dump's CREATE EXTENSION statements require one. Restore as a superuser role."

# Probe \restrict support as a capability: psql exits 0 on unknown meta-commands, so read output, not status.
DUMP_PG=$(in_app "$CONTAINER" sed -n 's/^-- Dumped by pg_dump version \([0-9][0-9.]*\).*/\1/p' "$REMOTE" | head -1)
[ -n "$DUMP_PG" ] || die "'$REMOTE' has no pg_dump version header - is it really a plain-SQL dump from this app?"
PSQL_PG=$(in_app "$CONTAINER" psql --version | grep -oE '[0-9]+\.[0-9]+' | head -1)
if in_app "$CONTAINER" grep -qE '^\\restrict ' "$REMOTE"; then
    PROBE=$(psql_t -d postgres -X -c '\restrict ul_probe' -c '\unrestrict ul_probe' 2>&1 || true)
    case "$PROBE" in
        *"invalid command"*)
            die "this dump uses \\restrict (pg_dump $DUMP_PG) and psql $PSQL_PG does not understand it. With ON_ERROR_STOP it aborts at line 5 having restored nothing; without it, it restores everything and exits 0 with the CVE-2025-8714 protection silently off. Restore from a container whose psql supports \\restrict." ;;
    esac
fi

EXISTS=$(psql_t -d postgres -tAc "select 1 from pg_database where datname = '$TARGET';")
if [ -n "$EXISTS" ]; then
    [ "$DROP_EXISTING" -eq 1 ] \
        || die "database '$TARGET' already exists. A non-empty target is what makes a restore fail at line 26 - pass --drop-existing to replace it, or pick another name."
    echo "==> dropping existing '$TARGET'"
    [ "$DRY_RUN" -eq 1 ] || psql_t -d postgres -c "DROP DATABASE \"$TARGET\";" >/dev/null
fi

echo "==> dump: $REMOTE (pg_dump $DUMP_PG, psql $PSQL_PG)"
echo "==> target: $TARGET on $DB_HOST:$DB_PORT as $DB_USER (live database '$LIVE_DB' untouched)"

if [ "$DRY_RUN" -eq 1 ]; then
    echo "==> --dry-run: every guard passed, stopping before psql"
    exit 0
fi

# template0, not template1: template1 may carry extensions that break the restore.
psql_t -d postgres -c "CREATE DATABASE \"$TARGET\" TEMPLATE template0 ENCODING 'UTF8';" >/dev/null

echo "==> restoring"
if ! psql_t -d "$TARGET" -v ON_ERROR_STOP=1 --single-transaction -f "$REMOTE" >/dev/null; then
    # Single transaction rolled back; drop the empty DB so it reads as failed, not good.
    psql_t -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET\";" >/dev/null 2>&1 || true
    die "restore failed; '$TARGET' was rolled back and dropped. Nothing was partially restored."
fi

TABLES=$(psql_t -d "$TARGET" -tAc "select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE';")
echo "==> restored $TABLES tables into '$TARGET'"
echo
echo "A table count is not proof the contents survived, and this script does not check that."
echo "Django will at least say whether it recognises the schema:"
echo "  docker exec -w /app/src/urbanlens -e UL_DB_NAME=$TARGET $CONTAINER \\"
echo "      /app/.venv/bin/python manage.py showmigrations --plan | tail -5"
echo
echo "bin/verify_backup_restore.sh proves the dump-and-restore *pipeline* is lossless, by"
echo "round-tripping the live database through its own scratch copies. It does not look at"
echo "'$TARGET', and takes no argument that would let it."
