#!/usr/bin/env bash
#
# Restore one of `core/controllers/backups/db.py`'s dumps into a scratch database.
#
# The dumps are plain SQL (`pg_dump -f`, no `-Fc`), and every obvious way to
# restore one is wrong in a way that does not announce itself. Measured against a
# real dump of this stack on 2026-09-05, all four of these were reproduced:
#
#   pg_restore backup_....sql
#     -> "input file appears to be a text format dump. Please use psql."
#        pg_restore cannot read a plain dump at all, and pg_restore is the only
#        restore example anywhere near this repo (`clone_prod_to_staging.sh` in
#        the infrastructure repo, which restores its own `-Fc` dump).
#
#   psql -d <a database created from template_postgis> -v ON_ERROR_STOP=1
#     -> exit 3, `ERROR: schema "tiger" already exists` at line 26, one table
#        restored out of 235. The dump installs PostGIS itself, so the target
#        has to be EMPTY - the opposite of what you would guess, and the
#        opposite of what PROBLEMS.md said until this script was written.
#
#   psql ... without ON_ERROR_STOP
#     -> exit 0. psql reports per-statement errors on stderr and still exits
#        successfully, so a restore that half worked looks like one that worked.
#
#   psql from the *database* container
#     -> "invalid command \restrict" at line 5. pg_dump 17.11 in the app image
#        emits `\restrict`, which the db image's psql 17.5 does not know. With
#        ON_ERROR_STOP that aborts having restored nothing; without it, exit 0
#        again. Restore from the app container, whose psql wrote the file.
#
# So this script creates the target itself from `template0` (emptiness by
# construction rather than by hope), checks the client can read what the server
# wrote, and runs psql with both `ON_ERROR_STOP=1` and `--single-transaction`,
# which together mean a failed restore leaves no database rather than half of one.
#
# It deliberately will not write to the live database. Restoring over a running
# deployment is a different procedure with different stakes - see docs/BACKUPS.md.
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

case "$TARGET" in
    [!a-zA-Z_]*|*[!a-zA-Z0-9_]*) die "target database name must be [A-Za-z_][A-Za-z0-9_]*, got '$TARGET'" ;;
esac

require_container

# Where the file lives. A host path is copied in; a bare name is expected to be
# in the container's backup directory already.
if [ -f "$BACKUP" ]; then
    REMOTE="/tmp/$(basename "$BACKUP")"
    echo "==> copying $BACKUP into $CONTAINER:$REMOTE"
    docker cp "$BACKUP" "$CONTAINER:$REMOTE" >/dev/null
else
    REMOTE="$BACKUP_DIR/$(basename "$BACKUP")"
    in_app "$CONTAINER" test -f "$REMOTE" \
        || die "'$BACKUP' is neither a file on this host nor a name in $BACKUP_DIR. Try --list."
fi

DB_USER=$(in_app "$CONTAINER" printenv UL_DB_USER)
DB_HOST=$(in_app "$CONTAINER" printenv UL_DB_HOST)
DB_PORT=$(in_app "$CONTAINER" printenv UL_DB_PORT)
DB_PASS=$(in_app "$CONTAINER" printenv UL_DB_PASS)
LIVE_DB=$(in_app "$CONTAINER" printenv UL_DB_NAME)

[ "$TARGET" != "$LIVE_DB" ] \
    || die "'$TARGET' is the live database this deployment serves. Restore into a scratch database and cut over deliberately - see docs/BACKUPS.md."

psql_t() { in_app -e PGPASSWORD="$DB_PASS" "$CONTAINER" psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" "$@"; }

# `CREATE EXTENSION postgis` in the dump needs a superuser, and finding that out
# 20 seconds into a restore during an incident is worse than finding it out now.
SUPER=$(psql_t -d postgres -tAc "select rolsuper from pg_roles where rolname = current_user;")
[ "$SUPER" = "t" ] \
    || die "role '$DB_USER' is not a superuser, and the dump's CREATE EXTENSION statements require one. Restore as a superuser role."

# The \restrict trap: a psql older than the pg_dump that wrote the file cannot
# read it, and says so in a way that is easy to miss (see the header).
DUMP_PG=$(in_app "$CONTAINER" sed -n 's/^-- Dumped by pg_dump version \([0-9][0-9.]*\).*/\1/p' "$REMOTE" | head -1)
[ -n "$DUMP_PG" ] || die "'$REMOTE' has no pg_dump version header - is it really a plain-SQL dump from this app?"
PSQL_PG=$(in_app "$CONTAINER" psql --version | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ "$(printf '%s\n%s\n' "$DUMP_PG" "$PSQL_PG" | sort -V | head -1)" != "$DUMP_PG" ]; then
    die "psql $PSQL_PG is older than the pg_dump $DUMP_PG that wrote this file; it will fail on \\restrict having restored nothing. Restore from a container whose psql is at least $DUMP_PG."
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

# template0, not template1: template1 is modifiable and a site that installed
# PostGIS into it would silently reintroduce the "schema already exists" failure.
psql_t -d postgres -c "CREATE DATABASE \"$TARGET\" TEMPLATE template0 ENCODING 'UTF8';" >/dev/null

echo "==> restoring"
if ! psql_t -d "$TARGET" -v ON_ERROR_STOP=1 --single-transaction -f "$REMOTE" >/dev/null; then
    # --single-transaction means nothing was committed, so the database is empty
    # rather than half-restored. Drop it so it cannot be mistaken for a good one.
    psql_t -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET\";" >/dev/null 2>&1 || true
    die "restore failed; '$TARGET' was rolled back and dropped. Nothing was partially restored."
fi

TABLES=$(psql_t -d "$TARGET" -tAc "select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE';")
echo "==> restored $TABLES tables into '$TARGET'"
echo
echo "A table count is not proof the contents survived. To check that:"
echo "  bin/verify_backup_restore.sh"
