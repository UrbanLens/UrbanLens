# Per-tier Postgres roles

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

## R29 — Every tier logs in as its own capped Postgres role, and only db-setup holds the owner

`id: R29` · `status: current` · `updated: 2026-09-15`

This implements decision 3 of D11 (`docs/designs/request-isolation-and-connection-budget.md`) for the compose
deployment, and closes what remained of P104.

**Who connects as what.** `services/core/database_roles.py` declares one `DatabaseRole` per `UL_PROCESS_ROLE`.
`db-setup`, a one-shot compose service, runs `init.py --db-only`: it creates the database, migrates it, then runs
`manage.py apply_database_roles`. Every service that connects to the database waits for it to exit 0. That command
converges the following, in one transaction:

- **`urbanlens_app`:** a NOLOGIN group holding DML on every table and sequence in `public`. Default privileges
  cover tables that a later migration creates, as soon as it creates them.
- **One `ul_<process role>` per tier:** LOGIN, a member of the group, with its `CONNECTION LIMIT`,
  `statement_timeout` and `idle_in_transaction_session_timeout`. None of SUPERUSER, CREATEROLE, CREATEDB,
  REPLICATION or BYPASSRLS.
- **Predefined roles, only where a tier needs them:**
  - `pg_read_all_stats` for web and websocket. The readiness probe counts backends by `backend_type`, which
    Postgres otherwise hides.
  - `pg_read_all_data` for bulk. The nightly `pg_dump` reads `tiger` and `topology`.

  Any predefined role outside those two is refused, and one no longer declared is revoked. A group member no
  longer declared loses LOGIN.

| Role | Limit | Deadline | Sized from |
|---|---|---|---|
| `ul_web` | 54 | 120s | 3 gunicorn workers × `--worker-connections 16` = 48. The other 6 are shared by `timeout_utils`' executor threads, which connect separately; there are 64 per worker |
| `ul_websocket` | 3 | 120s | Channels 4.3 runs every consumer's database call on one thread; plus the health probe |
| `ul_worker` | 5 | 3600s | prefork 4 + parent |
| `ul_bulk` | 4 | 3600s | prefork 2 + parent + `pg_dump` |
| `ul_panels` | 21 | 3600s | 20 threads + main |
| `ul_sandbox` | 5 | 3600s | media-worker 2 + 1, media-worker-batch 1 + 1 |
| `ul_ai` | 3 | 3600s | prefork 2 + parent |
| `ul_beat` | 1 | 120s | |
| `ul_metrics` | 1 | 120s | |

**The limits sum to 97**, which is `max_connections` 100 less `superuser_reserved_connections` 3. The owner is a
superuser, so those three slots are its own: a migration, an operator's `psql` or a restore still connects with
every tier at its limit. They protected nothing during the outage, because the app was the superuser.

**Two checks enforce the budget:**
- **`apply_database_roles`** refuses a set whose limits exceed the live server's non-superuser capacity.
- **`test_connection_budget_wiring`** refuses the same thing before a deploy. It also fails when:
  - a service's configured concurrency outgrows its role's limit;
  - a service logs in as another tier;
  - a service holds the owner's password;
  - a service starts before `db-setup`.

**Deadlines.** 120s matches nginx's `proxy_read_timeout` for the app. Celery tiers get `CELERY_TASK_TIME_LIMIT`.
D11 also named `idle_session_timeout` and `lock_timeout`, which are not set: `CONN_MAX_AGE=0` already closes idle
sessions, and `statement_timeout` bounds a lock wait.

**Passwords.** Every login role shares one password, `UL_DB_APP_PASS`, and only its SCRAM verifier reaches the
server. Staging and production refuse to apply roles without a password that differs from `UL_DB_PASS`. Since
every service waits for `db-setup`, a deploy missing it serves nothing, rather than handing the sandbox tier the
superuser's password. Other environments fall back to `UL_DB_PASS` with a warning. Separate passwords per tier
would isolate tiers from one another, but not from the owner, and are not done.

**What changes in practice:**
- **Tests in the app container fail:** `docker exec <app> pytest` no longer works, because `ul_web` cannot create
  a test database. `bin/run_tests.sh`, which runs as the owner against the test-runner's own test-db, is
  unaffected.
- **Manual migrations go through `db-setup`:** `docker compose up db-setup` (or `run --rm db-setup`) migrates.
  `docker exec <app> manage.py migrate` is refused permission.
- **The restore scripts read the owner's credentials from the db container:** `bin/restore_backup.sh` and
  `bin/verify_backup_restore.sh`.
- **perf_seed may skip `ANALYZE`:** its `ANALYZE` reports `False` when run as a tier role, since only the owner
  may analyze.
- **Dumps carry no grants:** the backup passes `--no-privileges`, so a dump restores into a cluster that has
  never had these roles. A restored database's tables are unreadable to the tiers until `db-setup` runs
  against it (`docs/BACKUPS.md`).

**Verified on `development_main`, 2026-09-15:**
- **Startup:** `db-setup` exited 0 having created all nine roles with the limits and settings above. app, app-ws,
  celery-worker, celery-worker-bulk, media-worker and celery-metrics each started as their own role with no
  restarts, and `pg_stat_activity` attributed the app's backend to `ul_web` / `urbanlens-web`.
- **Serving:** nginx answered `/` and `/health/` with 200. `manage.py migrate --check` as `ul_web` exited 0.
- **Backups:** the backup ran as `ul_bulk`. Without `--no-privileges` its dump held 475 `GRANT`/`REVOKE`
  lines, and restoring it into a cluster that lacks these roles (the test-db) aborted on
  `role "urbanlens_app" does not exist`. With it, the dump held none and restored all 237 tables there.
  `bin/verify_backup_restore.sh` and `bin/restore_backup.sh`, now running as the db container's owner, both
  completed.
- **Not started:** panels, beat, ai-worker and media-worker-batch, which were already stopped in that slot.

**Not measured, and not done:**
- **No load test:** the limits come from configured concurrency. X15's filter storm peaked at exactly 3 × 20
  web backends under the old flag; no storm has run against these limits. `ul_web` is the likeliest to fall
  short. A request that has already queried keeps its connection while it waits on an executor thread, and
  that thread's gateway call opens a second one to reserve its `ApiCallLog` row. Past 6 of those at once, the
  connection is refused and `_reserve_call` refuses the call with `RateLimiterUnavailableError`. The pin
  page's web search and media carousels, the Flickr and Immich pickers, and every other view that calls out
  now show their error card or degrade gracefully instead of 500ing (P122, fixed 2026-09-17 - not
  re-verified under an actual connection-limit storm, only under a mocked refusal). Nothing outside the
  web tier is affected.
- **The outage has not been reproduced:** its own shape, with slots held as the application's role, has still
  not been run (N20).
- **Kubernetes:** the infrastructure repo's k8s manifests still connect as the owner (N23).
- **No pooler:** pgbouncer stays deferred on D11's triggers. This bounds and attributes connections; it does
  not reuse them.
