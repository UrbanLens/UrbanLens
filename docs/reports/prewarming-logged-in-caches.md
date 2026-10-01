# Prewarming caches for logged-in users

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured on the date below. Re-run the measurement
> before relying on it, and rewrite this file when you do. When this file and the code disagree,
> the code wins.

`id: I6` · `status: actionable` · `updated: 2026-09-21`

Asked as part of the concurrency work behind P134: given that the app tier is what runs out at
1,000 users and the database is at a quarter of its cores, is there anything a logged-in user's
session could have ready before they ask for it?

Three candidates were considered against what the chrome actually reads. Only the third is worth
building, and it is not the one the question usually means.

## Prewarming at login moves the cost, it does not remove it

The cross-request access cache (`models/subscriptions/access_state.py`) is warmed by the viewer's
first authenticated page. Computing it during the login request instead would pay the same four
statements in the same request chain a few hundred milliseconds earlier, on the same worker, out
of the same four cores. There is nothing to win unless the work leaves the request path entirely,
and a Celery round trip for four indexed reads costs more than it saves.

The same holds for the settings singleton and the profile row: both are read once per request and
memoised for the rest of it, and both are one indexed lookup.

## What a logged-in user re-reads per page is already what cannot be cached

After the P134 work a warm map page is 14 statements. The chrome's share is the settings row and
the three badges - unread messages, unread notifications, and the active check-in banner. Those
are deliberately not cached, and prewarming them has the same objection caching them does: they
are the values a stale answer is most visible in, and `nav_active_checkins` is safety-critical. A
banner that hides an active check-in because a warm entry said so is a worse failure than the CPU
it saves.

## The case that is real: the herd after a global bump

One generation counter covers every account, so any act that changes access retires every
account's entry at once. Measured against the capacity population in `ul_perf_app`, incrementing
the counter and then re-requesting the map page:

| request | statements |
|---|---:|
| first page after the bump | **18** |
| every page after that | 14 |

So a bump costs each *active* viewer four extra statements, exactly once. At 1,000 concurrent
users that is about 4,000 extra statements arriving in the seconds after one admin action, on a
database measured at 0.91 of its four cores - absorbable, but it is a burst on the tier that is
*not* the bottleneck being paid for by the tier that is, because the recompute happens on a web
worker.

Prewarming that specific case - on commit of an access-bearing write, enqueue a task that
recomputes for the accounts seen recently - would move those 4,000 recomputes off the web workers
onto a Celery worker. Not built, for two reasons worth stating before anyone does:

- **It must run after the commit, for the same reason the bump does.** A task that recomputes from
  pre-commit rows and stamps them with the new generation reintroduces exactly the staleness the
  deferral in `forget()` exists to prevent, and this time for the full timeout.
- **"Recently seen" needs a source.** There is no cheap list of active accounts today; sessions
  are in the cache, `last_login` is too coarse, and walking users defeats the point.

## Not established

- Whether the herd is visible at all in a capacity run. The measurement above is one process on an
  idle container; whether 4,000 statements in a burst moves p95 at u1000 has not been tested, and
  a ladder run cannot easily inject an admin action mid-hold.
- Whether per-account counters would be better than one. They would make an account-scoped change
  (a subscription revoke) retire one entry instead of all of them, at the cost of a second read
  for site-wide changes, which are the common case.
