# `tests/perf/` — the neighbour load test

One question, asked directly: **does one account's ordinary use degrade another
account's?** Everything here is shaped by that being the question, rather than
"how fast is the site".

```
bin/run_perf_tests.sh --url http://localhost:21810 \
    --provision-container urbanlens_development_main_app \
    --db-container urbanlens_development_main_db --heavy-pins 20000
```

| file | what it is |
|---|---|
| `k6/neighbour.js` | The scenario. The neighbour's latency is the verdict; the actor's is a finding. |
| `k6/lib/schedule.js` | The timeline, written once. The actor's scenarios and the neighbour's phase tags are both derived from it. |
| `k6/lib/session.js` | Sign in once in `setup`, adopt everywhere. |
| `k6/lib/actions.js` | What the actor does, one export per acting phase. |
| `../../bin/run_perf_tests.sh` | Seeds, derives the budget, samples Postgres, returns a verdict. |
| `../../bin/perf/derive_budget.py` | Baseline p95 → the ceiling the measured pass is judged against. |
| `../../bin/perf/pg_activity_sampler.sh` | 1 Hz `pg_stat_activity` by role, for the failures latency cannot see. |

## Three things that are easy to get wrong here

**A load test that measures the sign-in page passes.** k6 resets a VU's cookie
jar between iterations, so a session installed once survives exactly one
request; afterwards every request is redirected to `/accounts/login/`, k6
follows the 302, and a fast 200 for the login page is recorded as a fast 200 for
the map. That happened here: the broken harness reported p95 72ms against this
dev stack, and a repeat of the same pass after the fix reported 243ms.
`checks{guard:signed_in}` is thresholded at `rate==1` so it cannot happen
quietly again.

**Signing in per VU is itself the load.** Password verification is PBKDF2 and
costs hundreds of milliseconds of CPU by design. Sixty VUs each signing in
completed zero iterations in fifty seconds while the same endpoints answered in
under 250ms one at a time.

**A known wedge has to be held constant or it is the only thing you measure.**
`Profile.compute_map_center` is O(n^2) in pins and sits on the map page's
critical path (P108). At 20,000 pins that is minutes during which the process
serves nothing, so the seeder stores the centre directly — the same value, in
one pass instead of n^2 — and `seed_heavy_account(..., precompute_map_center=False)`
puts it back when you want to reproduce P108 rather than work around it.

**A fixed millisecond budget is a claim about one machine on one day.** The
budget comes from a baseline pass minutes earlier on the same host. When the
baseline is already so slow that the absolute ceiling sets the budget instead of
the slack, `derive_budget.py` says so - a failure then says more about the host
than about the account under test.

## Run it against the real process model

The default dev environment runs `runserver` under daphne, and **the process
model changes the answer** - not by a little. Measured on the same account with
the same harness: one user filtering cost the neighbour 4,431 ms on daphne and
221 ms on gunicorn (X15). A run against `runserver` exercises the endpoints and
the harness honestly; it does not tell you what the deployment does.

```bash
cd ../infrastructure
python3 bin/dev_env.py create --name perf --branch <branch> \
    --environment staging --metrics --no-redata
```

`--environment staging` sets `UL_ENVIRONMENT=staging`, which is the only axis the
application branches on, so it gets gunicorn *and* is treated as a real
deployment by anything reading that variable. Two consequences worth knowing
before you run a load test on one:

- Set **`UL_ALLOW_OUTBOUND_APIS=false`** in its `.env`. Otherwise the import
  phase calls providers for real, thousands of times (P109). It is inherited
  automatically from this repo's own `.env`, which carries it.
- Set **`COMPOSE_PROFILES=metrics`** alongside `--metrics`, or the Celery
  exporter is silently absent (N15).

Containers are named `ul_<slug>_<service>`, so point the runner at
`--provision-container ul_perf_app --db-container ul_perf_db`.

**A `perf` environment already exists and is stopped**, not destroyed — its
checkout, images and registry entry are intact, so it costs nothing while idle
and starts in seconds:

```bash
cd /projects/environments/agents/perf/UrbanLens
docker compose -p ul-perf -f docker-compose.yml -f docker-compose.agent.yml start
```

Building it from scratch took three attempts and about half an hour, so prefer
starting this one. Stop it the same way when you are done. Two things it needs
that are easy to forget: the host wants the `development_main` stack stopped
during a run (the load generator, the target and 19 unrelated containers do not
fit comfortably together — see the operational note in the repo's own memory),
and `docker compose restart app` leaves nginx resolving a stale upstream, so
restart `nginx` alongside it or every request 502s.

## What it still cannot tell you

The load generator shares the host with the target, so its own CPU is part of
what the target competes with. The single-actor phases are the clean ones.
