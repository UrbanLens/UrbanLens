# Ask: a dev environment that runs gunicorn, and somewhere to put availability chaos

- **Status: OPEN as of 2026-09-10.** Two asks and one correction. Neither ask is
  urgent this week; both block the verification half of PL7, which is the work
  that would have caught the 11-hour connection outage before a user did.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N16` · `status: current`

**For whoever owns `UrbanLens/infrastructure`, not this repo.** Written
2026-09-10 against `release/v_0_8_0`. Nothing in your repository was edited to
produce this — the two `bin/` and `drills/` changes described below are requests,
not patches waiting to be merged.

Context in one paragraph. A map endpoint was 504ing on staging; the cause was
88% Python object construction and is fixed (R27). While fixing it we recorded
D11, which decides that one user's expensive request must be structurally unable
to reach another user's request — bounded worker pools, a separate pool for heavy
endpoints, and per-role Postgres connection budgets. PL7 is the programme that
implements it, and its first phase is *tests that state the invariant*. Two of
those tests cannot run anywhere that exists today, which is what this is about.

---

## Correction first: the Playwright suite is in this repo, not yours

We had been saying "the integration test suite is in `../infrastructure`". It is
not, and the mistake is ours. `tests/integration/` here holds the Playwright
projects (`smoke`, `services`, `api`, `ui`, `security`, `a11y`, `visual`), driven
by `bin/run_integration_tests.sh`. Your `tests/` is pytest over the ops tooling
(`test_devenv.py`, `test_promote.py`, …) and your `drills/` are shell.

Stating it so the split is written down somewhere: **application behaviour is
tested here; infrastructure behaviour and recovery are tested in your repo.** The
two asks below are the cases where those meet.

---

## Ask 1 — an opt-in way to get a dev environment that runs gunicorn

`bin/opslib/devenv.py:963` writes `UL_ENVIRONMENT: "development"` into every
generated `.env`, and the comment above it says exactly why:

> "development" is what makes init.py run Django's runserver with its
> autoreloader rather than gunicorn - which is the half of hot reload that does
> not need a bind mount.

That is a good default and we are not asking you to change it. The consequence,
though, is that **no environment anywhere exercises the process model that
production actually runs.** `src/bin/init.py:604-607` branches on that variable,
so a dev env runs `runserver`: single process, threaded, no gevent, no
`WEB_CONCURRENCY`, no `psycogreen`, no `worker_connections`. Our own pytest is no
better (N14): LocMemCache and an in-memory Celery broker.

So the topology that produced the outage — three gunicorn workers, each with
gevent's default **1000** greenlets, each greenlet able to hold its own Postgres
backend under `CONN_MAX_AGE=0`, all connecting as the same superuser against
`max_connections=100` — exists only in production and staging, which are the two
places we must not load-test.

**What we would like:** an opt-in flag on `dev_env.py create` — `--environment
staging`, or `--gunicorn` if you would rather keep the environment name and the
process model as separate axes — that writes `UL_ENVIRONMENT=staging` and
whatever else that path needs (`UL_UNSAFE_ALLOW_HTTP` appears to be one; the
staging banner suggests there may be more). Hot reload is not wanted in that
mode and its loss is the point.

**Second, smaller flag if it is cheap:** `--metrics`, setting
`UL_METRICS_ENABLED=true`, generating a `UL_METRICS_TOKEN`, and putting
`celery-metrics` in the compose profile. Related finding you may want anyway,
recorded here as N15: `urbanlens_staging_celery_metrics` has restarted **2,578
times** and is still restarting. It exits with `CommandError: UL_METRICS_ENABLED
is off; this exporter has nothing to serve`, which is correct behaviour for a
service that should not have been started — but nothing gates it, so
`docker compose up` starts it and `restart: unless-stopped` retries forever, each
attempt paying a full `django.setup()`. **Staging has had no Celery task metrics
for its entire life.** The one-line fix (`profiles: ["metrics"]`) is ours and is
in PL7 phase 2; the crash-looping container is on your host, so you should know
it is there.

---

## Ask 2 — where do availability chaos scenarios live?

We need four failure injections, and Jess has said they belong in your repo
alongside the drills rather than in our Playwright suite. We agree on the
location and want to flag one definitional problem before anyone writes them,
because your own `drills/README.md` draws the line in a place these sit on the
wrong side of:

> It is not a test suite. Tests check that code behaves; a drill checks that a
> *person* with a document can recover a system.

By that definition these are not drills. They inject an infrastructure failure
(your half) and then assert an application invariant holds (our half) — nobody
recovers anything, and there is no runbook being rehearsed. They are closer to
"tests check that code behaves", run against real infrastructure.

**So the question is yours to answer:** do these belong in `drills/` with the
definition widened, in a sibling directory (`chaos/`?) that reuses `guard.sh`, or
somewhere else entirely? We have no view beyond wanting them near your existing
guards, because every one of them is destructive on a host that also runs
production, and `guard.sh` is the thing that already knows that.

### The four, specified

Each targets one dev environment's containers (`ul_<slug>_*`), never
`urbanlens_production_*`, `urbanlens_staging_*` or `redata-production` — the same
rule your `DRILL_PRODUCTION_STACKS` list already encodes. All four restore in a
`trap` as well as on the happy path.

| # | Injection | What must hold | Today |
|---|---|---|---|
| 1 | `docker pause` the Valkey container | An already-signed-in user's page still renders; `/dashboard/map/pins/` answers 200 with `"cache": "miss"`; `/health/ready` reports the cache as errored and still returns 200 | **fails** — see below |
| 2 | Open idle connections as the app role until `pg_stat_activity` reads `max_connections - 3` | Requests fail *fast* with 503 and `Retry-After`, not a traceback; recovery within one probe interval after release | fails: unhandled `OperationalError` → 500 |
| 3 | `docker pause` the Celery worker | Pages load; an export reports itself queued rather than erroring; nothing 500s | expected to pass |
| 4 | Busy-loop inside the app container at its cgroup limit | A second user's p95 on a cheap authenticated page stays inside budget | fails until D11's `gthread` and `app-heavy` changes land |

Scenario 1 is worth reading rather than skimming, because the obvious
expectation is wrong in an interesting way. We assumed a Valkey outage 500s every
authenticated page, since sessions are `cached_db` over the stock `RedisCache`
with no error suppression. Reading Django 6.0.6's source says otherwise:
`cached_db.SessionStore.load()` and `.save()` both catch bare `Exception` and
fall through to the database, so **signed-in browsing keeps working**. What is
*not* guarded is `exists()` (`prefix + key in self._cache`) and `delete()` — and
`exists()` is on the login path via `create()`/`_get_new_session_key()`, while
`delete()` is on the logout path via `flush()`. Our own login lockout check
(`controllers/account.py:873`) is an unguarded `cache.get()` before either.

So the real failure profile is: **existing sessions browse fine, and nobody can
log in or out.** That is P105 here, unfixed, and scenario 1 is its reproduction.
Please do not "fix" it by making the cache fail open — the same unguarded calls
include the brute-force lockout counters, and failing those open would silently
disable rate limiting during exactly the incident when it matters.

### What the injection needs to be able to do

A thin `bin/chaos.py <slug> <scenario> [--for 120s] -- <command…>` would cover
all four: assert the slug's containers exist, assert none of the protected stack
names are involved, inject, run the command, restore in a `finally` and a trap.
We would call it from a wrapper here that runs the assertions. If you would
rather own both halves, that is fine too — the table above is the whole
specification, and we will supply the HTTP assertions in whatever form suits.

---

## Ask 3 — a long-lived environment for the load test, and Docker on its host

Separate from chaos, and the reason ask 1 matters. PL7 §4.1 specifies a *neighbour*
test: user B issues requests at a **fixed arrival rate** while user A runs one
expensive action at a time, and B's p95 must not move. It is the direct test of
the sentence this whole programme exists for — *"no action a user takes should
impact the availability of the site for other users, ever."*

k6 rather than Locust, and the reason is not preference: this needs an open-model
load. A closed-model tool lets a slowing server reduce B's request rate, which
hides precisely the degradation being measured.

The harness itself is application behaviour and lives here, in `tests/perf/`.
What we need from you:

- a long-lived environment created with ask 1's flag — `perf.dev.urbanlens.org`
  or similar — since seeding 20,000 pins takes a few minutes and should not be
  repeated per run;
- Docker access on whichever host it lands on, for two things the HTTP layer
  cannot see: seeding via `docker exec … manage.py`, and a 1 Hz
  `pg_stat_activity` sampler that counts backends by role and greps the database
  log for `53300` (`FATAL: sorry, too many clients already`). Both are hard
  pass/fail criteria alongside k6's own thresholds, because the resource that ran
  out in the outage was connections, not CPU;
- if you would rather the whole harness lived in your repo next to the drills,
  say so and we will move it — it is not written yet, which is the cheapest time
  to decide.

We will not point any of this at staging. Jess's standing rule is that damballa
is production, and a load test that saturates a connection pool is exactly the
thing that rule exists for.

---

## Two facts from our side you may want regardless

**Neither production nor staging is running what this branch says they run.**
`docker exec … ps` on both shows `gunicorn … -t 600 -k gevent -c gunicorn.conf.py`
with no `--max-requests`. Commit `f2623a5d4` changed that to `-t 180` with
recycling, and is committed but undeployed. Nothing on `release/v_0_8_0` is live.
No action wanted; it means a measurement taken against either host today is a
measurement of the older configuration.

**D11's connection budget, in case it affects anything you plan.** Ten Postgres
login roles replacing the single shared superuser, with `CONNECTION LIMIT`s
summing to 73 of the 97 usable slots (`ul_web` 20, `ul_panels` 20, `ul_web_heavy`
6, `ul_worker` 6, four at 4, `ul_ai` 3, `ul_beat` 2), applied by an idempotent
one-shot compose service on every `up`, plus role-level `statement_timeout` and
`idle_in_transaction_session_timeout`. `max_connections` stays at 100 —
deliberately, since each backend can allocate `work_mem` per sort node on top of
a several-MB baseline and the `db` container has a 2 GB limit. The equivalent for
the k8s site would be CNPG's `spec.managed.roles[]` plus a PreSync job for the
`ALTER ROLE … SET` parameters, but the k8s site is passive and we have not
touched it; the divergence is flagged, not resolved.

---

## What we are not asking for

The k8s manifests, `-t 600` included. `deployment-web.yaml` still carries the
timeout and worker args this repo has since changed, and D11 proposes both sides
call one `bin/start-web.sh` so the duplication stops. That is a real divergence
and it is on our list, not yours, until the compose side is proven.
