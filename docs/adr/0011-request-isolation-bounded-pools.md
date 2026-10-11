---
status: accepted
date: 2026-09-15
---

# One user's request must not reach another's: bounded pools with named budgets

Formerly `D11`.

Jess's requirement is that no action a user takes may affect the site's availability for other users. This is guaranteed by bounding every pool and naming its budget, not by making individual endpoints cheaper. The WSGI tier moved from gevent to gthread (3 workers × 4 threads), so connection demand is a chosen number and persistent connections work. Endpoints that can legitimately take seconds run in their own `app-heavy` pool, with per-session and total connection limits. Each process tier connects as its own Postgres role, with a connection limit and statement timeout. Celery work is split into queue classes (ADR-0013).

## Considered options

- Capping gevent's worker connections: shipped as an interim, but it bounds connections without giving users fair shares of the pool.
- A per-user request rate limit alone, or a per-request work budget in code: complements, not substitutes.
- pgbouncer: deferred until role limits sum past ~80, web replicas exceed 2, or connection setup shows in database CPU.

## Consequences

- Gunicorn's `-t` no longer bounds a request on the light tier; bounds come from the heavy pool, `statement_timeout`, and bounding the work itself.
- The membership of the heavy pool is the data file `config/heavy_routes.txt`.
