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

die() { echo "error: $*" >&2; exit 1; }

docker inspect "$CONTAINER" >/dev/null 2>&1 \
    || die "container '$CONTAINER' not found. Set UL_APP_CONTAINER or start the stack."

DB_USER=$(docker exec "$CONTAINER" printenv UL_DB_USER)
DB_HOST=$(docker exec "$CONTAINER" printenv UL_DB_HOST)
DB_PORT=$(docker exec "$CONTAINER" printenv UL_DB_PORT)
DB_PASS=$(docker exec "$CONTAINER" printenv UL_DB_PASS)
LIVE_DB=$(docker exec "$CONTAINER" printenv UL_DB_NAME)

pg() { docker exec -e PGPASSWORD="$DB_PASS" "$CONTAINER" "$@"; }
psql_t() { pg psql -U "$DB_USER" -h "$DB_HOST" -p "$DB_PORT" "$@"; }

cleanup() {
    [ "$KEEP" -eq 1 ] && { echo "==> --keep: left $SCRATCH_A and $SCRATCH_B in place"; return; }
    psql_t -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH_A\";" >/dev/null 2>&1 || true
    psql_t -d postgres -c "DROP DATABASE IF EXISTS \"$SCRATCH_B\";" >/dev/null 2>&1 || true
    pg rm -f "$DUMP_A" "$DUMP_B" 2>/dev/null || true
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
# heap order. Casting the whole row to text is what makes this cover geography,
# jsonb and bytea without naming a single column.
checksums() {
    local db="$1"
    local tables
    tables=$(psql_t -d "$db" -tAc "select table_name from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1;")
    [ -n "$tables" ] || die "no tables in '$db' - the restore did not land"
    local query
    query=$(printf '%s\n' "$tables" | awk '{printf "select '\''%s'\'' t, md5(coalesce(string_agg(x.r::text, '\''|'\'' order by x.r::text), '\'''\'')) c from public.\"%s\" x(r) union all ", $0, $0}' | sed 's/union all $/order by 1;/')
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
checksums "$LIVE_DB"   > /tmp/.ul_ck_live.$$
checksums "$SCRATCH_A" > /tmp/.ul_ck_a.$$
if diff -q /tmp/.ul_ck_live.$$ /tmp/.ul_ck_a.$$ >/dev/null; then
    echo "    identical across $(wc -l < /tmp/.ul_ck_a.$$) tables"
else
    echo "    DIFFERS in $(diff /tmp/.ul_ck_live.$$ /tmp/.ul_ck_a.$$ | grep -c '^<') tables:"
    diff /tmp/.ul_ck_live.$$ /tmp/.ul_ck_a.$$ | grep '^<' | cut -d'|' -f1 | sed 's/^< /      /'
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
     '\\\\x00ff10de'::bytea, 1234567890.0123456789),
    (NULL, NULL, NULL, NULL, NULL, NULL, NULL);
" >/dev/null

echo "==> second hop: $SCRATCH_A -> dump -> $SCRATCH_B"
dump_to "$SCRATCH_A" "$DUMP_B"
restore_into "$SCRATCH_B" "$DUMP_B"

checksums "$SCRATCH_A" > /tmp/.ul_ck_a2.$$
checksums "$SCRATCH_B" > /tmp/.ul_ck_b.$$
STATUS=0
if diff -q /tmp/.ul_ck_a2.$$ /tmp/.ul_ck_b.$$ >/dev/null; then
    echo "    identical across $(wc -l < /tmp/.ul_ck_b.$$) tables, probe included"
else
    echo "    FIDELITY FAILURE - both databases were quiescent, so this is the format losing data:"
    diff /tmp/.ul_ck_a2.$$ /tmp/.ul_ck_b.$$ | head -20
    STATUS=1
fi
rm -f /tmp/.ul_ck_live.$$ /tmp/.ul_ck_a.$$ /tmp/.ul_ck_a2.$$ /tmp/.ul_ck_b.$$

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
