# X16 — The four chaos scenarios, run for the first time: one catastrophic, three clean

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: X16` · `status: holds` · `updated: 2026-09-10`

PL7 §4.5 predicted what should still work while one piece of the infrastructure is broken, and which
of those predictions would fail today. This is the first time any of it has been run. Target: a
`--environment staging` dev environment (gunicorn + gevent, the production process model), probed
with an **already-established** session.

| scenario | prediction | measured |
|---|---|---|
| **cache-outage** (Valkey paused) | signed-in browsing keeps working; readiness degraded-but-200 | **every request 500s after ~32s, readiness included** |
| worker-outage (celery paused) | pages load, nothing in the request path needs a worker | clean — readiness 200, page renders, pins answer |
| cpu-saturation (every allowed core spun) | neighbour degrades but serves | clean — readiness 200, page renders, pins answer |
| connection-exhaustion (100/100 backends) | fast 503 + `Retry-After`, not a traceback | served normally; **see the caveat** |

## The one that matters

**cache-outage is far worse than P105 claimed**, and P105 has been rewritten around the measurement.
The entry said signed-in browsing would keep working, reasoning correctly from which Django session
paths are wrapped. What it did not reason about was time:

```
  /health/ready                    500 in 32.16s
  /dashboard/map/pins/?limit=5     500 in 32.15s
  /dashboard/map/                  500 in 32.17s
```

`socket_timeout` is 2s, so ~32s means roughly sixteen cache operations per request, each waiting its
own timeout, serially. Whole-site throughput during a Valkey outage is about two requests a second,
and the readiness endpoint fails the same way — which is the concrete case behind the readiness
warning sent to infrastructure in N20.

## Why "clean" is worth less than it looks for connection-exhaustion

The two expectations tagged `P104` passed, and that should not be read as P104 being wrong.

The injection here held Postgres slots **as `postgres`, an external superuser**, until 100 of 100
were taken. The app kept serving, including an uncached `bbox` query that must reach the database —
because the app's own connections were already open and were never the ones taken.

`chaos.py`'s own scenario description is more faithful: *"Hold Postgres connections **as the
application's own role** until three slots are left."* P104 was the application exhausting the pool
with its own connections and then starving itself; an external party filling the pool is a different
shape. This run does not reproduce it, and the scenario should be re-run through `chaos.py` once
that tool can dispatch (N20).

One thing the run did establish: the first version of the probe passed this scenario while querying
nothing at all. `map_pins_json` caches the profile's whole unbounded root-pin set, so the plain
endpoint answered `cache=hit` and touched no connection. The probe now uses a `bbox` request, which
is explicitly `cacheable=False`. A scenario that cannot fail is worse than no scenario.

## What running it cost

`chaos.py inject` cannot dispatch at all (N20) — its subparser `dest` collides with its own `command`
positional, so `args.command` is never the subcommand name. All four scenarios here were injected by
hand (`docker pause`, held `pg_sleep` sessions, a spin loop in the container), each with an explicit
restore, which is precisely the guarantee that tool exists to provide and which hand-injection does
not.

The probe itself needed two fixes before it measured anything real, both recorded in its own
docstrings: it signed in *during* the injection rather than before it, which tests the sign-in path
instead of the premise; and it used `http.cookiejar`, which appends `.local` to a dotless host, so
every cookie set by `localhost` was stored under `localhost.local` and never sent back.

## Not run

The scenarios were run one at a time against an otherwise idle environment. None was run *underneath*
the neighbour load, which is where PL7 §4.5's cpu-saturation expectation actually lives — "one user's
expensive work against another user's cheap page" is a statement about contention, and an idle box
cannot answer it.
