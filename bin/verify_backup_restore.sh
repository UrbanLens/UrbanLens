#!/usr/bin/env bash
#
# Prove that a backup this app writes can actually be restored, and that nothing
# is lost on the way through.
#
# A restore procedure nobody has run is a belief, not a capability, and the day
# it is first exercised is the worst possible day to discover it is wrong. This
# script exercises it on an ordinary day, against a real dump from the real
# code path, and compares content rather than trusting an exit status - psql
# exits 0 even when individual statements failed, so "it ran" proves nothing.
#
# It never writes to the live database. It reads it once (pg_dump takes a
# consistent snapshot) and does everything else in scratch databases it creates
# and drops.
#
# Two comparisons, because they catch different things:
#
#   live -> scratch1        Every table's full content, hashed. On a quiet
#                           system this must match exactly. On a busy one a
#                           difference may just be a write that landed after the
#                           dump's snapshot, so this one is reported, not fatal.
#
#   scratch1 -> scratch2    A second dump/restore hop, both ends quiescent, so a
#                           difference here cannot be explained by concurrent
#                           writes and is a real fidelity failure. This hop also
#                           carries a probe table seeded with the types most
#                           likely to survive a naive round trip badly -
#                           geography, jsonb, bytea, numeric, timestamptz.
#
# The hashes are of whole rows across every schema the dump carries. Both halves
# of that were wrong when this was first written, and the check passed anyway -
# see the comment on checksums().
#
# Usage:
#   bin/verify_backup_restore.sh            # verify, then clean up
#   bin/verify_backup_restore.sh --keep     # leave the scratch databases behind
#
# Environment:
#   UL_APP_CONTAINER   app container name (default urbanlens_development_main_app)

set -euo pipefail

CONTAINER="${UL_APP_CONTAINER:-urbanlens_development_main_app}"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

SCRATCH_A=ul_restore_verify_a
SCRATCH_B=ul_restore_verify_b
DUMP_A=/tmp/ul_restore_verify_a.sql
DUMP_B=/tmp/ul_restore_verify_b.sql
# Everything this writes on the host goes here, so the trap removes it whatever
# path the script exits by. The first version wrote /tmp/.ul_ck_*.$$ and deleted
# them only on the success path, which left a table-by-table digest of the live
# database behind on every failure.
WORK=$(mktemp -d)

die() { echo "error: $*" >&2; exit 1; }

docker inspect "$CONTAINER" >/dev/null 2>&1 \
    || die "container '$CONTAINER' not found. Set UL_APP_CONTAINER or start the stack."

DB_USER=$(docker exec "$CONTAINER" printenv UL_DB_USER)
DB_HOST=$(docker exec "$CONTAINER" printenv UL_DB_HOST)
DB_PORT=$(docker exec "$CONTAINER" printenv UL_DB_PORT)
LIVE_DB=$(docker exec "$CONTAINER" printenv UL_DB_NAME)

# The password is read inside the container from its own environment rather than
# passed in with `docker exec -e PGPASSWORD=...`, which puts it in the host's
# process table where any user can read it out of `ps`. `_` fills $0.
# shellcheck disable=SC2016  # the single quotes are the point: $UL_DB_PASS expands in the container, not here
pg() { docker exec "$CONTAINER" sh -c 'export PGPASSWORD="$UL_DB_PASS"; exec "$@"' _ "$@"; }
psql_t() { pg psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" "$@"; }

cleanup() {
    rm -rf "$WORK"
    [ "$KEEP" -eq 1 ] && { echo "==> --keep: left $SCRATCH_A and $SCRATCH_B in place"; return; }
    pg rm -f "$DUMP_A" "$DUMP_B" 2>/dev/null || true
    # Said out loud rather than swallowed. Each scratch database is a complete
    # copy of the live one, so a drop that fails - most often because something
    # is still connected to it - leaves a full second copy of production data
    # sitting on the server under a name nobody is watching.
    local db
    for db in "$SCRATCH_A" "$SCRATCH_B"; do
        if ! psql_t -d postgres -c "DROP DATABASE IF EXISTS \"$db\";" >/dev/null 2>&1; then
            echo "warning: could not drop scratch database '$db' - it is a full copy of '$LIVE_DB'." >&2
            echo "         Drop it by hand: psql -c 'DROP DATABASE \"$db\"'" >&2
        fi
    done
}
trap cleanup EXIT

# The same flags core/controllers/backups/db.py uses, so this verifies the format
# that is actually on disk rather than a more convenient one.
dump_to() { pg pg_dump -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" -w "$1" -f "$2"; }

restore_into() {
    psql_t -d postgres -c "DROP DATABASE IF EXISTS \"$1\";" >/dev/null
    psql_t -d postgres -c "CREATE DATABASE \"$1\" TEMPLATE template0 ENCODING 'UTF8';" >/dev/null
    psql_t -d "$1" -v ON_ERROR_STOP=1 --single-transaction -f "$2" >/dev/null
}

# Hash every table's entire contents, ordered so the result does not depend on
# heap order.
#
# `x`, with no column alias list. The first version said `x(r)` and hashed
# `x.r`, which is not the row: a table alias with a column alias list renames
# the *leading* columns, so `r` was the first column and every hash was of that
# column alone - primary keys, for most tables. It compared equal after wiping
# every other column of every table, and the geography/jsonb/bytea probe below
# reduced to md5 of its two serial ids. A whole-row reference is what actually
# covers those types without naming a column.
#
# Every schema the dump carries, not just `public`: PostGIS puts 36 more tables
# in `tiger` and `topology`, and a restore that lost them compared clean.
checksums() {
    local db="$1"
    local tables
    tables=$(psql_t -d "$db" -tAc "select table_schema || '.' || table_name from information_schema.tables where table_schema not in ('pg_catalog', 'information_schema') and table_type = 'BASE TABLE' order by 1;")
    [ -n "$tables" ] || die "no tables in '$db' - the restore did not land"
    local query
    query=$(printf '%s\n' "$tables" | awk -F. '{printf "select '\''%s'\'' t, md5(coalesce(string_agg(x::text, '\''|'\'' order by x::text), '\'''\'')) c from \"%s\".\"%s\" x union all ", $0, $1, $2}' | sed 's/union all $/order by 1;/')
    psql_t -d "$db" -tAc "$query"
}

echo "==> dumping live database '$LIVE_DB'"
dump_to "$LIVE_DB" "$DUMP_A"
DUMP_BYTES=$(pg stat -c %s "$DUMP_A")

echo "==> restoring into scratch '$SCRATCH_A'"
restore_into "$SCRATCH_A" "$DUMP_A"
TABLES=$(psql_t -d "$SCRATCH_A" -tAc "select count(*) from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE';")
echo "    $TABLES tables from a ${DUMP_BYTES}-byte dump"

echo "==> comparing live -> $SCRATCH_A"
CK_LIVE="$WORK/live" CK_A="$WORK/a" CK_A2="$WORK/a2" CK_B="$WORK/b"
checksums "$LIVE_DB"   > "$CK_LIVE"
checksums "$SCRATCH_A" > "$CK_A"
if diff -q "$CK_LIVE" "$CK_A" >/dev/null; then
    echo "    identical across $(wc -l < "$CK_A") tables"
else
    # `|| true` on both: diff exits 1 when the files differ, pipefail promotes
    # that to the pipeline's status, and a bare failing pipeline under `set -e`
    # kills the script. So the branch this comment calls "reported, not fatal"
    # aborted the run before naming a single table.
    echo "    DIFFERS in $(diff "$CK_LIVE" "$CK_A" | grep -c '^<' || true) tables:"
    diff "$CK_LIVE" "$CK_A" | grep '^<' | cut -d'|' -f1 | sed 's/^< /      /' || true
    echo "    (a write landing after the dump's snapshot explains this on a busy system;"
    echo "     the quiescent hop below is the one that cannot be explained away)"
fi

# Types that a round trip is most likely to mangle, in a table the app does not
# own, so this hop tests the format rather than whatever the app happens to hold.
echo "==> seeding a type probe into '$SCRATCH_A'"
psql_t -d "$SCRATCH_A" -v ON_ERROR_STOP=1 -c "
CREATE TABLE public._restore_probe (
    id serial PRIMARY KEY,
    txt text,
    js jsonb,
    ts timestamptz,
    pt geography(Point, 4326),
    poly geography(MultiPolygon, 4326),
    raw bytea,
    num numeric(20, 10)
);
INSERT INTO public._restore_probe (txt, js, ts, pt, poly, raw, num) VALUES
    (E'unicode é中 quote'' backslash\\\\ newline\n', '{\"a\": [1, 2.5, null], \"b\": {\"c\": true}}', '2026-09-05 22:29:54.123456+00',
     ST_SetSRID(ST_MakePoint(-73.7562, 42.6526), 4326)::geography,
     ST_Multi(ST_Buffer(ST_SetSRID(ST_MakePoint(-73.7562, 42.6526), 4326)::geography, 100)::geometry)::geography,
     decode('00ff10de', 'hex'), 1234567890.0123456789),
    (NULL, NULL, NULL, NULL, NULL, NULL, NULL);
" >/dev/null

echo "==> second hop: $SCRATCH_A -> dump -> $SCRATCH_B"
dump_to "$SCRATCH_A" "$DUMP_B"
restore_into "$SCRATCH_B" "$DUMP_B"

checksums "$SCRATCH_A" > "$CK_A2"
checksums "$SCRATCH_B" > "$CK_B"
STATUS=0
if diff -q "$CK_A2" "$CK_B" >/dev/null; then
    echo "    identical across $(wc -l < "$CK_B") tables, probe included"
else
    echo "    FIDELITY FAILURE - both databases were quiescent, so this is the format losing data:"
    diff "$CK_A2" "$CK_B" | head -20 || true
    STATUS=1
fi

# Not `migrate --check`: that asserts the tree is fully migrated, which is a fact
# about the deployment, not about the restore - a database legitimately behind on
# migrations fails it before and after. What must hold is that Django can read
# the restored copy and finds it in the same migration state it read from live.
echo "==> checking Django reads the restored schema as it reads live"
plan() {
    docker exec -w /app/src/urbanlens -e UL_DB_NAME="$1" "$CONTAINER" \
        /app/.venv/bin/python manage.py showmigrations --plan 2>/dev/null | grep -E '^\[[ X]\]'
}
plan "$LIVE_DB"   > /tmp/.ul_plan_live.$$ || true
plan "$SCRATCH_A" > /tmp/.ul_plan_a.$$ || true
if [ ! -s /tmp/.ul_plan_a.$$ ]; then
    echo "    Django could not read '$SCRATCH_A' at all"
    STATUS=1
elif diff -q /tmp/.ul_plan_live.$$ /tmp/.ul_plan_a.$$ >/dev/null; then
    echo "    same migration state as live ($(grep -c '^\[X\]' /tmp/.ul_plan_a.$$) applied, $(grep -c '^\[ \]' /tmp/.ul_plan_a.$$) pending)"
else
    echo "    migration state DIFFERS from live:"
    diff /tmp/.ul_plan_live.$$ /tmp/.ul_plan_a.$$ | head -10
    STATUS=1
fi
rm -f /tmp/.ul_plan_live.$$ /tmp/.ul_plan_a.$$

[ "$STATUS" -eq 0 ] && echo "==> restore verified" || echo "==> restore NOT verified"
exit "$STATUS"
