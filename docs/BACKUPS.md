# Database Backups and Restore

What `core/controllers/backups/db.py` writes, how to restore it, and the four ways a restore
of these files fails while looking like it worked.

Every claim below was measured on 2026-09-05 against a real dump of the
`development_main` stack, produced by the application's own `DatabaseBackup.run()`. The
round trip is re-runnable: `bin/verify_backup_restore.sh`.

## What is on disk

`pg_dump -U <user> -h <host> -p <port> -w <db> -f <path>` - **plain SQL**, no `-Fc`, written to
`<backups_dir>/backup_<YYYYMMDD>_<HHMMSS>.sql`. The dump is written to a `.tmp` path and renamed
only on success, so a dump killed mid-write leaves an obviously-partial file rather than a
truncated one under a real backup name. Retention keeps `settings.backup_retention` files (30 in
this environment) and reaps `.tmp` files older than a day.

The dump contains `CREATE SCHEMA tiger`, `CREATE SCHEMA tiger_data` and `CREATE SCHEMA topology`
**without** `IF NOT EXISTS`, followed by `CREATE EXTENSION IF NOT EXISTS` for postgis,
postgis_tiger_geocoder, postgis_topology, fuzzystrmatch and pg_trgm. Both halves of that shape
matter, and they pull in opposite directions - see below.

## Restoring

```bash
bin/restore_backup.sh --list                                    # what is available
bin/restore_backup.sh backup_20260905_060200.sql my_scratch_db  # restore into a new database
```

The script creates the target itself and refuses to touch the live database. By hand, from inside
the **app** container - not the database container, and as a **superuser**:

```bash
export PGPASSWORD="$UL_DB_PASS"
psql -U "$UL_DB_USER" -h "$UL_DB_HOST" -p "$UL_DB_PORT" -d postgres \
     -c "CREATE DATABASE restored TEMPLATE template0 ENCODING 'UTF8';"
psql -U "$UL_DB_USER" -h "$UL_DB_HOST" -p "$UL_DB_PORT" -d restored \
     -v ON_ERROR_STOP=1 --single-transaction -f /app/src/backups/backup_20260905_060200.sql
```

`-h` and `-p` are not optional, and that is not pedantry: the app container runs no PostgreSQL of
its own, so a bare `psql -U postgres` dies on `/var/run/postgresql/.s.PGSQL.5432: No such file or
directory` before doing anything. An earlier revision of this document omitted them.

## The four ways this goes wrong

Each was reproduced against a real dump; the exit statuses and messages are verbatim.

**1. `pg_restore` cannot read these files at all.**

```
$ pg_restore -f /dev/null backup_20260905_222248.sql
pg_restore: error: input file appears to be a text format dump. Please use psql.
```

This matters more than a wrong-tool error usually would, because `pg_restore` is the only restore
example anywhere near this repository: `bin/clone_prod_to_staging.sh` in the sibling
`infrastructure` repo restores a `-Fc` dump it creates for itself. An operator reaching for the
nearest example reaches for the wrong tool.

**2. The target must be EMPTY, not PostGIS-ready.**

This is the opposite of the intuition, and `docs/PROBLEMS.md` asserted the intuition until this
document replaced it. The dump installs PostGIS itself, so a target that already has it collides:

```
$ createdb restored -T template_postgis && psql -d restored -v ON_ERROR_STOP=1 -f backup_....sql
psql:backup_20260905_222248.sql:26: ERROR:  schema "tiger" already exists
$ echo $?
3
```

One table restored out of 235. Use `template0` - not `template1`, which is modifiable and on a
host where somebody installed PostGIS into it would reintroduce exactly this failure.

**3. `psql` exits 0 when statements fail.**

Without `ON_ERROR_STOP=1`, the same collision reports three errors on stderr and still exits
successfully, restoring all 235 tables (the extensions are `IF NOT EXISTS`, so only the three
`CREATE SCHEMA` statements fail and the rest proceeds). That is the dangerous case: it happens to
work, so the habit of omitting the flag survives to the restore where something real fails. Always
pass `ON_ERROR_STOP=1`, and `--single-transaction` alongside it so a failure leaves no database
rather than half of one.

**4. The database container cannot restore the app container's dumps.**

`pg_dump` 17.11 in the app image emits `\restrict` (a psql meta-command added in 17.6). The
database image ships PostgreSQL 17.5, whose psql does not know it:

```
$ docker exec <db-container> psql -d restored -v ON_ERROR_STOP=1 -f backup_....sql
psql:backup_20260905_222248.sql:5: error: invalid command \restrict
$ echo $?
3
```

Zero tables restored. Without `ON_ERROR_STOP` the same command exits **0** having restored all 235
tables, reporting only that one line and a matching `\unrestrict` at the end.

That second outcome is worse than the confusing failure it looks like. `\restrict` is the fix for
CVE-2025-8714, added in PostgreSQL 17.6 (2025-08-14): it stops psql executing meta-commands that
appear in the dump, so a superuser on the machine the dump came from cannot run arbitrary commands
on the machine restoring it. A client that does not understand `\restrict` reports an error and
carries on **without that protection**. Restore from the container whose psql wrote the file.

`bin/restore_backup.sh` compares the dump's `pg_dump` version against the running `psql` and refuses
when the client is older - but be clear about which case that covers. It is *a container configured
with `UL_DB_*` whose psql has fallen behind the dump*. It is **not** "you pointed the script at the
database container": that container has neither a `UL_DB_*` environment nor a backups directory, so
the script fails well before the version check. The guard is real; it is not what stops you reaching
for the wrong container.

## What the round trip actually proved

`bin/verify_backup_restore.sh`, run 2026-09-05:

- 235 public tables, 861,888-byte dump, restored into a `template0` database with
  `ON_ERROR_STOP=1 --single-transaction`: exit 0, empty stderr.
- Every table's full contents hashed (`md5(string_agg(x::text, ...))` over a whole-row reference,
  which covers geography, jsonb and bytea without naming a column) - **identical across all 271
  tables**, live vs restored. 271, not 235, because the comparison covers every schema the dump
  carries: PostGIS puts 36 more tables in `tiger` and `topology`.
- A second hop with both ends quiescent, carrying a probe table of the types most likely to be
  mangled - `geography(Point,4326)`, `geography(MultiPolygon,4326)`, `jsonb`, `bytea`, `numeric`,
  `timestamptz`, non-ASCII text with embedded quotes and backslashes, and an all-NULL row -
  **identical across all 272 tables**. A seeded point came back as `POINT(-73.7562 42.6526)` at
  SRID 4326.
- Django reads the restored copy in the same migration state it reads live.

The script's teeth were checked by breaking it three ways: building the target from
`template_postgis` fails at line 26; deleting a probe row between the hops reports `FIDELITY
FAILURE` naming the table; and wiping every non-key column of the probe - same row count, same
primary keys - does too.

**That third check is here because the first version of this document claimed all of the above
while proving almost none of it.** The hash was written `string_agg(x.r::text ...) FROM t x(r)`,
and `x(r)` is a table alias *with a column alias list*: it renames the first column, so `x.r` was
that column, not the row. Every hash was of primary keys. The geography/jsonb/bytea probe reduced
to md5 of its two serial ids, and a restore that lost every other column of every table compared
clean. The row-deletion teeth check passed because removing a row does change a first-column
aggregate - it exercised one of the few failures the broken hash still caught.

Note that it does **not** assert `migrate --check` passes. That asserts the deployment is fully
migrated, which is a fact about the deployment rather than about the restore - this environment has
5 pending migrations and fails it identically before and after. What must hold is that the restored
copy is in the *same* state as the source.

## Why plain SQL rather than `-Fc`

`-Fc` would compress, allow selective and parallel restore, and make `pg_restore` - the tool the
neighbouring repo already demonstrates - the correct one. It was not adopted because the format is
not where the danger was: every failure above is a procedure or environment mismatch, and all four
are now either guarded by `bin/restore_backup.sh` or checked by `bin/verify_backup_restore.sh`.
Changing the suffix would also orphan the backups currently on disk and require moving
`BACKUP_FILENAME_RE`, `is_backup_temp_filename` and retention together.

The one real argument for it is failure 4: a custom-format dump has no meta-commands, so the
client-version skew that breaks `\restrict` does not arise. If that skew recurs - or if dump size
starts to matter, at 30 retained uncompressed copies - revisit this.

## Restoring over a live deployment

Deliberately not scripted. `bin/restore_backup.sh` refuses to write to the database named by
`UL_DB_NAME`, because doing so safely means stopping every writer first (app, app-ws, and all
Celery workers), and a script that stops production services is a worse thing to have lying around
than a documented sequence:

1. Stop every service configured with `UL_DB_*`. Enumerated from `docker-compose.yml` rather than
   remembered, because the obvious list is wrong: it is `app`, `app-ws`, `celery-beat`,
   `celery-worker`, `celery-worker-panels`, **`celery-metrics`**, **`media-worker`**,
   **`media-worker-batch`** and **`ai-worker`** - nine, four of which do not match `celery-worker*`.
   Re-derive it rather than copying this line, since a service added later will not be in it:

   ```bash
   python3 -c "import yaml;d=yaml.safe_load(open('docker-compose.yml'));print(' '.join(n for n,c in d['services'].items() if any(str(k).startswith('UL_DB_') for k in (c.get('environment') or {}))))"
   ```

2. Restore into a scratch name with `bin/restore_backup.sh` and confirm the table count.
3. Swap the names, **connected to neither of them**:

   ```bash
   psql -d template1 -c 'ALTER DATABASE "<live>" RENAME TO "<live>_before_restore";' \
                     -c 'ALTER DATABASE "<scratch>" RENAME TO "<live>";'
   ```

   Two constraints make this the only shape that works, and both bite silently if ignored.
   PostgreSQL answers `ERROR: current database cannot be renamed` for the database the session is
   connected to - and in this deployment the live database is named `postgres`, which is exactly
   where a bare `psql` lands, so the naive form fails on its *first* statement. Run it from the
   scratch database instead and the first statement succeeds and the second fails, which leaves
   nothing under the live name at all. Hence `template1`, or any third database.

   It also needs zero other sessions on either database (`ERROR: database "x" is being accessed by
   other users`), which is what step 1 is for. Both statements in one `psql` invocation so the
   window between them is as small as it can be. Keeping the displaced database rather than dropping
   it is what makes the step reversible.
4. Start the services and check the site before dropping anything.

Renaming rather than dropping-and-restoring means the failure mode is "we are back where we
started", not "there is no database".
