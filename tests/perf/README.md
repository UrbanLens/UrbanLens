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

## P123: does the neighbour's search degrade as the labels table grows?

The neighbour's rotation includes two `search.panel` requests, `global_search_match` and
`global_search_miss` (see `docs/PROBLEMS.md`'s P123 for the mechanism: a cross-account label-table
scan with no access scoping). Neither needs an actor phase to reproduce - the defect is triggered by
total row count in `dashboard_labels`, not by anything happening in real time - so growing that
table is a seeding step, not a scenario:

```
bin/run_perf_tests.sh --url http://localhost:21810 \
    --provision-container urbanlens_development_main_app \
    --db-container urbanlens_development_main_db \
    --heavy-pins 20000 --heavy-labels 50000
```

`--heavy-labels N` grows `dashboard_labels` to `N` rows on the heavy account via
`perf_seed.py`'s `seed_bulk_labels` - bulk-created, unattached to any pin/image/wiki, and topped up
rather than restarted on a re-run. Unattached is deliberate: the scan this reproduces happens before
any join to another model narrows it, so what the rows are attached to has no bearing on the cost.
Omit the flag (or pass 0) and the two search endpoints still run, they just measure whatever the
target's `dashboard_labels` already holds - useful for confirming the endpoints work before
committing to a large seed, not for judging the defect.

### The same mechanism, five more relations

`docs/PROBLEMS.md`'s P123 entry generalises the same unscoped semi-join to
`ArticleSearchProvider`'s `pin__aliases__name`/`wiki__aliases__name`, `TripSearchProvider`'s
`activities__title`/`activities__notes`/`comments__text`, and `SafetySearchProvider`'s
`messages__body` - confirmed at the unit level, and re-measured at HTTP scale the same way as labels
(see `docs/PROBLEMS.md`'s "Integration-level reproduction of the five generalised relations" entry:
60-second idle-phase pass at 50,000 rows each, comfortably inside budget, connection pool untroubled).
`--heavy-search-relations N` grows all five relations (`PinAlias`, `WikiAlias`,
`TripActivity`, `TripComment`, `SafetyCheckinMessage`) to `N` rows each, on one dedicated host row
per relation (a pin, its wiki, a trip, a check-in - each foreign key is NOT NULL, unlike `Label`,
so a host is unavoidable, but which host is as immaterial to the cost as labels' being unattached):

```
bin/run_perf_tests.sh --url http://localhost:21810 \
    --provision-container urbanlens_development_main_app \
    --db-container urbanlens_development_main_db \
    --heavy-pins 20000 --heavy-labels 50000 --heavy-search-relations 50000
```

No new k6 requests were needed: `GlobalSearchEngine.search` already fans one query out across every
provider, so the same `global_search_match`/`global_search_miss` requests exercise these five
relations too, once seeded. `SEARCH_MATCH_TERM` changed from `"Perf Bulk Label"` to `"Perf Bulk"` so
it matches both `seed_bulk_labels`' and `seed_bulk_search_relations`' name/prefix - a strict
substring of the old term, so an existing `--heavy-labels`-only run measures identically; the change
only starts to matter once `--heavy-search-relations` is also passed.

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
`Profile.compute_map_center` was O(n^2) in pins and sat on the map page's
critical path — at 20,000 pins, minutes during which the process served nothing.
That is what this harness found on its first honest run (P108, fixed: the
calculation is now linear and costs ~99 ms at that size). The seeder still
stores the centre directly, because holding a known cost constant is worth doing
whether or not it is currently large, and `precompute_map_center=False` leaves
it to be computed the way a real first page load would.

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

## The capacity test asks a different question

The neighbour test asks what one account costs another. `k6/population.js` asks
how many people the deployment serves at once:

```bash
bin/run_capacity_tests.sh --url http://localhost:31000 \
    --provision-container ul_perf_app --db-container ul_perf_db \
    --container-prefix ul_perf_ --nginx-container ul_perf_nginx --population 1000
```

| file | what it is |
|---|---|
| `k6/population.js` | Ramps through levels of concurrent users, each browsing, polling and holding a notification socket. |
| `k6/lib/capacity.js` | The timeline, the journey weights, think time and budgets. Tested by `capacity.test.js`. |
| `../../bin/run_capacity_tests.sh` | Provisions the population, runs the samplers, returns a verdict. |
| `../../bin/perf/container_sampler.sh` | cgroup CPU, throttling and memory for every container of the stack, read from the host. |
| `../../bin/perf/report_capacity.py` | One table per hold: endpoints against budget, containers, the proxy's errors. |

`provision_integration_env --population N` creates `e2e-load-*` accounts sized
from a heavy-tailed distribution (60% under 100 pins, 1% at 10,000-20,000),
befriended, with conversations, notifications and visits, and writes a manifest
of **minted sessions** rather than passwords. Minting is what makes a thousand
users possible: at ~1 s of PBKDF2 each, signing them in would measure the hasher.

Each VU sends its own `X-Forwarded-For`, so per-visitor proxy limits see a
thousand visitors rather than one; nginx trusts that header from the Docker
bridge the way it trusts it from the tunnel.

What a VU does is modelled on the templates, not guessed: a page load is the page
and whatever it fetches as it renders (the header's unread counts and safety
banner come inside the page), the map downloads its document on a cold pin cache
before polling meta, a filter claims the pin store with the fingerprint meta
served (so the server answers with identifiers, as it does a browser whose store
is complete), an open page polls unread messages every
60 s and the map's meta every 2 min, and every page opens `/ws/notifications/`
and closes it on the next navigation. The messages page's own socket, saved-filter
counts and anything a user writes are not modelled yet.

## What it still cannot tell you

The load generator shares the host with the target, so its own CPU is part of
what the target competes with. The single-actor phases are the clean ones.
