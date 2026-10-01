# N24 — Playwright's default worker count exhausts `ul_web`'s 54-connection cap and prints as unrelated 500s, not as a connection error

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: N24` · `status: current` · `updated: 2026-09-16` · `source: session of 2026-09-16 browser-verifying X21 on urbanlens_development_main; role limit independently confirmed this session via psql, the rest carried from the same session's Playwright run and not independently re-run here`

This is about `urbanlens_development_main` specifically (`docs/notes/database-roles.md`, R29, applies
to every compose deployment, but the numbers below are this slot's).

## The role cap, and why it bites under Playwright

`ul_web` - the role the app container's threaded ASGI dev server connects as, since R29's per-tier
rollout - has `CONNECTION LIMIT 54` (`max_connections` 100 on the server). Confirmed this session:

```
$ docker exec urbanlens_development_main_db psql -U postgres -c '\du' | grep ul_web
 ul_web        | 54 connections
```

The dev server holds roughly 36 of those idle at rest. Running the Playwright suite at its default
4 workers pushes past 54 fast enough that the pool refuses new connections mid-run. One such run
(carried from this session, not re-run independently here) produced 81
`psycopg2.OperationalError ... FATAL: too many connections for role ul_web`, surfacing as HTTP 500s
on endpoints that have nothing to do with each other or with the change under test: `POST pins/`,
`POST trips/`, `/dashboard/vault/`, `/dashboard/tools/`, and all five every-page "shell fragment"
partials (notifications unread-count and dropdown, messages unread-count and dropdown, the safety
nav-banner). **These read exactly like application regressions and are not** - the tell is that they
cluster across unrelated views and fragments rather than around whatever the change under test
touched.

**What worked, same run:** stop the Celery/AI/media worker containers (each holds its own idle pool
against other roles, and shutting them down frees headroom on the shared server), restart the app
container to drop its own idle pool, and pass `--workers=1` to Playwright. Failures fell from 30 to
19 and passes rose from 70 to 82 on the concurrency change alone - no application code changed
between the two runs.

**Not established:** the exact worker count where this starts failing (only 1 and the default were
tried), whether `--workers=2` is safe, and whether this is specific to `development_main`'s specific
container mix or general to any compose slot running the default worker set.

## Two smaller gotchas hit the same session

- **`media_nginx` in this slot cannot start while `ul_perf` is running.** Both publish port 21801
  (`docker-compose.yml:308`, `UL_MEDIA_PORT` default), and whichever started second fails to bind.
  Confirmed the port default is shared by both compose profiles; did not reproduce the actual bind
  failure this session.
- **Provisioning accounts for the suite:**
  `docker exec <app> /app/.venv/bin/python /app/src/urbanlens/manage.py provision_integration_env
  --out /tmp/e2e.json`, then point `UL_E2E_ACCOUNTS_FILE` at a copy on the host. Confirmed this
  session: `manage.py` and `provision_integration_env` both exist at that exact path
  (`src/urbanlens/dashboard/management/commands/provision_integration_env.py`); did not run it end
  to end.

## Related

`docs/notes/database-roles.md` (R29) already documents the same role rollout's effect on **pytest**:
`docker exec <app> pytest` cannot create a test database at all now (`ul_web` has no `CREATEDB`),
where `bin/run_tests.sh` against the `test_runner` container is unaffected. This note is the
**browser**-testing analogue: pytest fails loudly and immediately on this cap; Playwright degrades
under concurrency and prints as scattered application 500s instead.
