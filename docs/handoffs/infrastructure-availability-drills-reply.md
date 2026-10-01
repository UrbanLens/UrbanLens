# Reply: both corrections hold, the broker finding changes our scenario table, and N15's count is confirmable

- **Status: SENT, 2026-09-10.** Answers the infrastructure repo's reply to
  `infrastructure-availability-drills-and-gunicorn-dev-envs.md` (N16). Two of
  their corrections to our ask are right and are recorded as such; one claim
  about our branch is stale; one of their findings invalidates part of the
  scenario table we sent, and is recorded against D11 and P105 rather than left
  in this thread.
- **Direction: outbound.** This repo to whoever owns `UrbanLens/infrastructure`.
- `id: N17` · `status: current`

**For whoever owns `UrbanLens/infrastructure`, not this repo.** Written
2026-09-10 against `release/v_0_8_0`. Every claim below was re-checked against
this repository's code before being written; nothing in your repository was
edited.

---

## Your two corrections to our ask are right

**`--gunicorn` as a separate axis does not exist.** Confirmed: `src/bin/init.py`
branches on the environment name and nothing else —

```python
if self.environment == "development":
    self.run_dev_server()
else:
    self.run_prod_server()
```

— so a flag promising to vary the process model independently of the
environment name would have promised something the app cannot honour.
`--environment {development,staging}` is the right shape. Our ask offered the
alternative as if it were free; it was not.

**`DJANGO_SECRET_KEY` is mandatory outside development, and we did not name it.**
Confirmed at `settings/base.py:47-63`: absent, it raises `ImproperlyConfigured`
at import for any environment that is not development or a test run, with the
reasoning that an ephemeral key orphans anything already written to an encrypted
field. Every container in a staging-flavoured environment would have restart
looped, and the ask would have read as broken rather than as incomplete. Good
catch, and the same goes for tracing `UL_UNSAFE_ALLOW_HTTP` to an actual
redirect loop rather than listing it as a thing to set.

## One claim about our branch is stale

> Their `profiles: ["metrics"]` fix isn't on `release/v_0_8_0`, so I gated the
> exporter from our override instead

It is now — `docker-compose.yml` on `origin/release/v_0_8_0` carries
`profiles: ["metrics"]` on `celery-metrics`, pushed in `7f88bf428` and still
present at `b9d5be597`. If your override also gates it, the two are
idempotent and no action is needed; worth knowing so the override can be
retired rather than kept as a permanent divergence.

**N15's 2,578 is a count, not only a mechanism.** You could not reach damballa;
we could, and did:

```bash
ssh damballa 'docker inspect urbanlens_staging_celery_metrics --format "{{.RestartCount}}"'
# 2549   (first reading)
# 2578   (a few minutes later, still climbing)
```

So it is measured, and it was still climbing while measured. Production's
`celery_metrics` we did **not** verify — the read-only probe returned nothing
for it and production is not a scratch environment. Treat that half as unknown.

## Your broker finding invalidates part of the table we sent, and we have taken it

> `CELERY_BROKER_URL` falls back to `VALKEY_URL` (`base.py:352`), so pausing
> valkey removes the cache and the broker. Their table treats those as separate
> scenarios.

Confirmed, at `settings/base.py:374` on this branch (line numbers moved under
our own edits):

```python
CELERY_BROKER_URL = os.getenv("UL_CELERY_BROKER_URL") or VALKEY_URL or "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = os.getenv("UL_CELERY_RESULT_BACKEND") or CELERY_BROKER_URL
```

so with `UL_CELERY_BROKER_URL` unset — which is the default and what a dev
environment gets — one instance is the cache, the channel layer, the session
store, the result backend **and** the broker.

Two consequences, both ours to own:

1. **Scenarios 1 and 3 in our table are not independent.** We wrote scenario 1
   ("Valkey paused") expecting `map.pins` to answer 200 with `cache: "miss"`,
   and scenario 3 ("worker paused") separately for the task path. Pausing
   Valkey does both at once: `safely_enqueue_task` cannot reach a broker
   either, so a scenario-1 run also exercises the enqueue-failure path and its
   expectations were written as if it would not. Expect it to surface more than
   we predicted, and read a failure there as our table being wrong before
   reading it as the app being wrong.
2. **It strengthens the split we had already decided.** D11 §2.6 splits Valkey
   into a broker (`noeviction`, persistent) and a cache (`allkeys-lru`, 8 GB of
   the 10 GB Jess allocated). We had justified that on the *fill* case — the
   TTL'd population, which is the map cache and the channel layer, is evicted
   first and then every write fails OOM. Your finding is the *outage* case, and
   it is the sharper one: today a cache being unavailable is the broker being
   unavailable, and there is no configuration in which that is intended.
   Recorded against D11 rather than left in this thread.

We have not changed the scenario table itself — the ask is frozen at its send
date per this directory's convention. Take this reply as the amendment.

## Your "neither" on where chaos lives

No argument. `bin/` over a `drills/`-sibling for one script is your call to
make, and parsing `DRILL_PRODUCTION_STACKS` out of `guard.sh` rather than
restating it is better than what we would have asked for — a restated list is a
list that drifts, and an unreadable guard being a refusal rather than a fallback
is the right failure direction for something that can reach production.

"A restore that does not verify fails the run even when the command passed" is
the part we would have got wrong.

## One thing we cannot see from here, and one still-live hazard

**We cannot see the code**, and we looked twice, in both checkouts on chiron:

| | `/projects/UrbanLens/infrastructure` | `/projects/environments/dev/UrbanLens/infrastructure` |
|---|---|---|
| `bin/chaos.py`, `bin/opslib/chaos.py` | absent | absent |
| `--environment` flag in `dev_env.py` | absent | absent |
| newest commit | `9beb986`, 2026-09-09 | `371227b`, 2026-09-02 |
| uncommitted changes | none | none |

`find /projects /home -name chaos.py` returns nothing, `git fetch` reports
nothing newer, and `docs/plans/compose-era-tooling.md` is the 2026-09-06 version
with four items and no #6 or #7. `_NAMED_SERVICES` is still the nine-service
tuple carrying the comment you identified as false.

So none of the above is a review of your implementation — only of the reasoning
you described, which is why this reply argues with none of your design choices.
Tell us where it is pushed and we will read it properly.

**`_NAMED_SERVICES` still pins 9 of 19 here.** In the checkout we can see,
`bin/opslib/devenv.py:300` lists nine services and its comment says anything
unlisted "keeps compose's own project-prefixed name, which is already unique per
project". Our `docker-compose.yml` defines nineteen. We mention it only because
you flagged fixing it and the fix is not visible here: until it is, a
staging-flavoured environment created from this state is the hazard you
described, and the blast radius is a real stack on damballa.

## What we still owe you

Nothing blocking. When the tooling is reachable we will point `tests/perf/`'s k6
scenario at an environment made with `--name perf --environment staging` and use
`chaos.py sample` rather than building our own `pg_stat_activity` sampler — that
was on our plan and is now deleted from it.
