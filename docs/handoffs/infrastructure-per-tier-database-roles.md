# Ask: `UL_DB_APP_PASS` before the next staging or production deploy, and per-tier roles for k8s

- **Status: SENT, 2026-09-15.**
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N23` · `status: current`

**For whoever owns `UrbanLens/infrastructure`, not this repo.** Written 2026-09-15 against `release/v_0_8_0`.
Nothing in your repository was edited to produce this.

Context in one paragraph. P104's 11-hour outage read 97/100 connections held, with every container logging in as
the superuser, so no tier had a budget and the reserved slots protected nothing. The compose stack now logs each
tier in as its own `ul_<UL_PROCESS_ROLE>` with a `CONNECTION LIMIT` (R29, `services/core/database_roles.py`). A
one-shot `db-setup` service creates those roles, and it alone holds the owner's credentials. Here is what that
asks of you.

1. **Staging's and production's `.env` need `UL_DB_APP_PASS`, distinct from `UL_DB_PASS`, before either next
   deploys this branch.** Without it `db-setup` exits non-zero, and nothing that connects to the database starts.
   That is deliberate: the alternative hands the sandbox tier the superuser's password. `UL_DB_USER` and
   `UL_DB_PASS` stay the owner's, so `opslib/staging.py`'s refresh, which reads them from the env file, is
   unaffected.
2. **`db-setup` is a new service in the app family.** A rebuild that recreates services by name should include
   it, and `compose up -d` runs it on every up. The assumption behind `opslib/devenv.py`'s `_SLOW_FIRST_BOOT_SERVICES`,
   that `app` migrates on first boot, no longer holds: `db-setup` migrates, and `app` starts after it exits.
3. **`migrate --check` still works; `migrate` itself does not.** `docker exec <app> manage.py migrate --check`
   (in `staging.py`) only reads `django_migrations`, which `ul_web` may do. Running `migrate` from any serving
   container is refused permission; use `docker compose up db-setup`.
4. **k8s parity:**
   - **Grant the roles after migrating.** `job-migrate.yaml` runs `migrate` with the owner's secret; add
     `manage.py apply_database_roles` after it. That needs a superuser, because it grants `pg_read_all_stats`
     and `pg_read_all_data`.
   - **Switch each Deployment's credentials.** Move its `UL_DB_USER` to the role named by its
     `UL_PROCESS_ROLE`, with `UL_DB_APP_PASS` as the password.
   - **Re-size for replicas.** `ul_web`'s 54 is sized for one app container. More web replicas need smaller
     per-pod concurrency or new limits, and D11 names more than 2 replicas as its pgbouncer trigger.
5. **The `connection-exhaustion` chaos scenario (N20)** can now hold slots as `ul_worker` and assert that `ul_web`
   keeps serving. That is the outage's shape, which has never been run.
