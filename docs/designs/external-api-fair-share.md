# D14 — A shared service budget is divided by who is actually competing for it, not by the user count

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: D14` · `status: accepted` · `updated: 2026-09-12`

> **The measurement half is built; the limiter half is not.** `accepted` records
> the direction, which is Jess's, not a sign-off on the parameters in
> *Choosing the numbers* — those want real traffic before anyone picks them.

## What was wrong with the obvious fix

H34 in the 2026-09-11 availability audit is real: external-API budgets
(`services/core/rate_limiter.py`) are enforced app-wide with no per-user
dimension, so one account's ordinary use can exhaust a feature for everybody.
The historical-map tile proxy is the sharpest case — it silently inherits the
20/min, 500/day default.

The obvious fix is a fixed per-user cap, and Jess rejected it, correctly:

> Setting hard limits per user sounds like it will create throttling problems
> for the average user in order to protect the unused portion of the per-service
> quota in a lot of cases. For instance: many of our quotas are 500 queries per
> hour; if we have 500 users, setting their quota to 1 per hour will result in
> the site being throttled for 100 active users, while 400 queries per hour go
> unused.

That is the whole objection in one number. A fixed cap is sized for the worst
case — everyone active at once — and the worst case is not the normal case, so
the normal case pays for it. It also fails in the other direction: 500 users
with a cap of 1 is useless the day the site has 5,000 users, because the cap
was derived from a population that changes.

## The direction

> Measuring use of each service over time, so we can anticipate usage and adjust
> as demand increases would be a better solution. Therefore, if demand is low, a
> single user could use nearly all the quota, but if demand is high, they'd be
> limited to protect the ability of other users to use it.

The share is a function of live contention, not of registered accounts. One
person alone on a service gets essentially all of it. A hundred people competing
for it each get a floor that the heaviest user cannot eat.

## Step 1, built 2026-09-12: attribution

None of this was measurable. `ApiCallLog` recorded *that* a call happened —
service, endpoint, timing, cost — and nothing about whose behalf it was made
on. When a quota ran out, the system could not name who had spent it. Neither
the fixed cap nor the fair share could be evaluated, because both need a
per-consumer number that did not exist.

`ApiCallLog.profile` now carries it, read from the actor
`WriteSourceMiddleware` already binds for every authenticated request
(`models/abstract/versioning.py`), so no call site had to learn about it.

Two properties the limiter below depends on, both tested in
`test_api_calls_are_attributed.py`:

- **Refusals are attributed too.** The rows the limiter writes for calls it
  *blocked* are the record of demand that went unmet, and "whose demand was it"
  is exactly the question when a budget runs out.
- **Background work is attributed to nobody.** `celery.py` binds
  `WriteSource.AUTOMATIC` with no actor, so the site's own scheduled calls
  stay unattributed rather than being charged to whichever profile happened to
  be nearby. `usage_by_profile` and `active_consumers` exclude them, so they
  never restrain a person.

The known gap: a Celery task started *by* a user also runs as `AUTOMATIC`, so
its calls are unattributed. For a pin import — thousands of calls from one
person's press — that undercounts the very consumption this is meant to see.
Closing it means threading the requesting profile onto the task, which is a
separate change (and one the H21 payload work touches the same tasks for).

Reporting, on `ApiCallLogQuerySet`, composable with `for_service`/`billable`
so a caller chooses whether refusals count:

- `usage_by_profile(window)` → `(profile_id, calls)`, heaviest first.
- `active_consumers(window)` → distinct attributed profiles.

## Step 2, not built: the admission rule

Sketch, to be argued with before it is written:

```
allowed(user, service):
    remaining = budget - used_total(window)
    if remaining <= 0:                       return False   # today's rule, unchanged
    others   = active_consumers(window) - (1 if user used it else 0)
    floor    = reserved_per_consumer(service)
    if remaining > others * floor:           return True    # slack: nobody is competing
    return used_by(user, window) < remaining / max(others, 1)
```

The first branch is what delivers Jess's requirement: while the budget has
slack, nothing is restrained, and a lone user reaches the whole quota. The
reserve only bites once enough people are competing that the remainder would
not cover their floors.

Costs one extra indexed `COUNT` per call for `used_by(user)`; `used_total` and
`active_consumers` are per-service, not per-user, so they belong in a
short-TTL cached snapshot rather than being recomputed per call — recomputing
a `GROUP BY` over `ApiCallLog` on every outbound request would be its own
availability problem, of the kind H48 already records for the costs page.

### Choosing the numbers

`reserved_per_consumer` is the only free parameter and it should come from
measurement, not from a guess: once a few weeks of attributed traffic exist,
the floor worth reserving is roughly the median session's consumption of that
service, and the window should be the one the budget is expressed in. Until
that data exists, any number here would be the same kind of invention the
fixed cap was.

### What this deliberately does not do

- **It does not replace the app-wide budget.** That is the spend cap and stays
  exactly as it is; fair share only decides who gets the remainder.
- **It does not apply to unattributed work.** A background sweep is the site
  spending its own budget, and throttling it in favour of a user would stall
  safety check-ins to render a map tile.
- **It does not make a service free when demand is low.** `remaining > 0`
  is still the outer gate, so the spend ceiling is unchanged.

## See also

- `docs/notes/availability-audit-2026-09-11.md` — H34, and H48 for why the
  aggregate belongs in a cache.
- [D11](request-isolation-and-connection-budget.md) — the same requirement one
  tier down: a bounded pool with named budgets rather than a per-user cap.
