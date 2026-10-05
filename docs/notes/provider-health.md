# PL10 — UrbanLens notices a provider that stops answering it, backs off, and tells a person

> **Written by a Claude agent. Not authoritative.**
>
> This records what one automated session measured or believed on the date
> below. It was not independently reviewed, its numbers may be stale, and the
> code may have moved. Re-run the measurement before relying on it, and
> **rewrite this file** when you do — do not add a correction underneath the
> old claim. When this file and the code disagree, the code wins.

`id: PL10` · `status: live` · `updated: 2026-10-05`

Jess's requirement (2026-10-05): every external provider is watched for refusing or failing, background work backs
off automatically, and a person is told, so that a provider that stops answering is never noticed only when someone
goes looking. REData's half is REData's PL13 (its `provider-health.md` design), with a problem record beside it of
the providers that were refusing or failing, unnoticed, on 2026-10-05. This is the UrbanLens half, the same design on
UrbanLens's own machinery.

## What was missing

The rate limiter caps how often a provider is asked. `upstream_breaker` honours a wait REData or Wayback names. Neither
judged a provider by what it had *done*, so a provider answering 500 or timing out was called at its full budget,
and nothing told anyone.

Replaying the judge's rules (below) over dev's `dashboard_api_call_log` for 28 Sep – 4 Oct, hour by hour, eight
providers would have been backed off for 32 provider-hours between them. `redata_api` alone spent 14 of those hours
answering 84 of 2,105 calls (dev calls production REData, which answered 503). `redata_places` answered 0 of 121, and
`cloudflare_image_classifier` 0 of 68. Over the whole week, 869 of the 3,302 REData calls dev sent were answered 5xx.

## How it works

`services/core/provider_health.py` is the whole mechanism. Its docstring is the reference; in short:

- **Outcomes** come from columns `ApiCallLog` already has: 401/403/429 refused, 404/410 empty, any other 4xx or 5xx
  failed, no status with `success=False` failed (a timeout or dead connection), otherwise ok. Calls refused here
  before sending (rate limited, disabled, geo-filtered) and reservations still in flight are left out. No column was
  added.
- **Judging** (`judge`), per rate-limiter service key, on the last hour, or the last day for a rarely called one:
  - backed off for refusing at 3+ refusals making up half the calls;
  - backed off as failing at 20% or fewer answered;
  - degraded below half its 7-day normal answer rate, or at a much higher empty share.

  It needs at least 10 calls.
- **Backoff and probing**: the backoff is 15 minutes, doubling per consecutive backoff, capped at 24 hours. Then the
  provider probes: three calls decide whether it recovered or backs off again. The level resets after a day healthy.
- **The gate** (`check_admission`) runs in `_RateLimitedSession._do_request` after the breaker, and in
  `api_call_slot`. It reads a cache snapshot the evaluator writes, memoised for 15 seconds, and fails open. A refusal
  is an `UpstreamThrottledError` logged `was_rate_limited`, with `record_unanswered`, exactly like a tripped breaker,
  so `outages_observed` callers never store it as "nothing here".
- **Background versus live** (`services/core/background_work.py`): `UrbanLensTask` binds each run by the queue it was
  delivered from.
  - **Background**: `bulk`, `maintenance`, `sandbox_batch` and Celery's default queue. Refused while the provider is
    backed off, with one probe per 2 minutes while it is probing.
  - **Live**: everything else, including `panel_fetch`, `interactive` and `ai`, and anything outside a task. Live
    calls keep a trickle of 3 per provider per 5 minutes.

  The queue was chosen over the write actor because every task binds `WriteSource.AUTOMATIC` with no actor, panel
  fetches included.
- **Alerting**: one digest per evaluation through `notify("provider_health", ...)`. It goes out after 30 minutes
  unhealthy, as a reminder every 24 hours, and as a recovery notice. It is off under `DEBUG` and in tests.
  - A degraded provider that then backs off is reported again at once. A degraded or probing provider that nothing
    calls any more is cleared after a day, and reported once as called too rarely to judge, rather than reminded
    about forever.
  - Routing is `SiteSettings.notify_provider_health_email` and `notify_provider_health_gotify`. Both are on by
    default, because the point is that nobody has to remember to turn it on.
  - The subject names REData when at least 3 of its services are failing, and the outbound network when at least half
    of 5+ other providers are. A provider counts as failing when it is backed off or probing as failing, or had at
    least 10 mostly unanswered calls in the hour. A refusal does not count: a dead network times out.
- **Visibility**: a Provider health card at the top of Site admin › API Rate Limits. It lists every provider not
  healthy, with why, since when, until when, its last answer and the calls refused today. It says so when the evaluator
  has not run for 30 minutes.
- **Schedule**: the `provider-health-evaluation` beat entry runs every 5 minutes on `maintenance` under a 240-second
  overlap lock.

## Open

1. **Gotify credentials** for UrbanLens come from `SiteSettings` (`UL_GOTIFY_URL` / `UL_GOTIFY_TOKEN` by default),
   which Jess sets per deployment. Until they are set the digest goes to email only, and no SMTP relay is
   configured on the LAN yet either.
2. **A 404 counts as answered.** That is right for a point lookup, so a provider whose every tile now 404s (dev's
   `google_open_buildings`) is degraded only against its baseline, never backed off. A per-service override of
   the outcome map would fix it if one is wanted.
3. **No per-host scope.** REData splits a mirror service by host. No UrbanLens service fans out to unrelated hosts
   under one key, so none is split.
4. **Thresholds are REData's**, which were chosen for one residential IP shared by every host on the LAN. Production
   UrbanLens has its own address and may want a gentler live trickle. They are module constants.
