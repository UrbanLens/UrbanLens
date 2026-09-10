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

**A fixed millisecond budget is a claim about one machine on one day.** The
budget comes from a baseline pass minutes earlier on the same host. When the
baseline is already so slow that the absolute ceiling sets the budget instead of
the slack, `derive_budget.py` says so - a failure then says more about the host
than about the account under test.

## What this cannot tell you yet

A development environment runs `runserver`, not gunicorn, so **no dev target
reproduces the process model the invariant actually depends on** - worker class,
worker count, and the connection budget that follows from them. Runs against one
exercise the endpoints and the harness; they do not exercise the topology. That
needs a staging-like environment (`dev_env.py create --environment staging`).
