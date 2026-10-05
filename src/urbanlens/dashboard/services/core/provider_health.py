"""Notice when an external provider is refusing or failing this deployment, back off, and tell a person.

The rate limiter caps how often a provider is asked, and ``upstream_breaker`` honours a wait a provider names. Neither
judges a provider by what it has *done*: a provider answering 500, timing out or refusing every call is called at the
full budget until someone goes looking. On 2026-10-05 REData was answering 503 to a quarter of the calls the dev
deployment made to it, and nothing had said so.
This is the UrbanLens half of REData's provider health (REData's PL13; this repository's PL10).

## How

Every outbound call writes an ``ApiCallLog`` row. :func:`evaluate_provider_health` (beat, every five minutes) reads the
last day of rows, derives what each call's source did from its status (:data:`OUTCOME_EXPRESSION`), and judges each
provider - each rate-limiter service key - with :func:`judge`:

- **Refusing**: at least :data:`MIN_REFUSALS` refusals (401, 403, 429), and refusals at least :data:`REFUSAL_SHARE`
  of the calls. Backed off.
- **Failing**: at most :data:`ANSWERED_FLOOR` of the calls answered (5xx, timeouts, failed connections). Backed off.
- **Below its normal**: answering under half as often as over the previous week, or answering empty far more often.
  Degraded, which is alerted on but pauses nothing.

The window is the last hour, or the last day for a provider called too rarely to judge on an hour. A backed-off
provider stays off for :func:`backoff_duration` (15 minutes, doubling with each consecutive backoff, capped at a day),
then is **probed**: a few calls go through, and their answers decide whether it recovered or backs off for longer.

## Who is stopped

:func:`check_admission` is the gate, called by ``rate_limiter._RateLimitedSession`` on every gateway call and by
``rate_limiter.api_call_slot`` for SDK calls. It reads a snapshot the evaluator leaves in the cache, so a call pays a
cache read at most every few seconds and never a query, and an unreadable cache lets the call go.

- **Background work stops first** (``background_work.is_background``: a task on the bulk, maintenance or batch
  queues). It is refused while its provider is backed off, and gets one probe every :data:`PROBE_INTERVAL_SECONDS`
  while it is probing.
- **Live work keeps a trickle**: :data:`LIVE_TRICKLE_CALLS` per provider per :data:`LIVE_TRICKLE_WINDOW_SECONDS`, so a
  page can still be answered and the source is still being asked whether it is back.

A refusal is an ``UpstreamThrottledError`` and is recorded with ``outages.record_unanswered``, the same as a tripped
``upstream_breaker``, so nothing downstream stores it as "nothing here".

## Telling a person

A provider unhealthy for :data:`ALERT_AFTER` is listed in one digest per evaluation through ``notify`` (the
``provider_health`` event; ``SiteSettings.notify_provider_health_*`` route it), then again every
:data:`ALERT_REMIND_AFTER` while it lasts, and once more when it recovers.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from django.conf import settings as django_settings
from django.core.cache import cache
from django.db import DatabaseError
from django.db.models import Case, CharField, Count, DateTimeField, F, Func, Max, Q, Value, When
from django.utils import timezone

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.models.provider_health.meta import BackoffCause, ProviderState
from urbanlens.dashboard.services.core.background_work import is_background
from urbanlens.dashboard.services.core.locks import beat_lock

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.provider_health.model import ProviderHealth

logger = logging.getLogger(__name__)

#: Anything the cache can raise when it cannot answer; ``RuntimeError`` is the test suite's network guard.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)

#: The window a verdict is read from when it holds enough calls.
SHORT_WINDOW = timedelta(hours=1)
#: The window for a provider called too rarely to judge on :data:`SHORT_WINDOW`.
LONG_WINDOW = timedelta(hours=24)
#: Granularity of the counts the evaluator reads. A window starts on a bucket boundary, and a bucket that straddles a
#: window's start is left out, so this is how much of a window's first minute can go uncounted - probes included.
BUCKET_SECONDS = 60
#: Fewest calls a window must hold before anything is concluded from it.
MIN_SAMPLE = 10
#: Fewest refusals that back a provider off, however small the window.
MIN_REFUSALS = 3
#: Share of a window's calls that must be refusals to back a provider off for refusing.
REFUSAL_SHARE = 0.5
#: A window in which this share or less was answered backs the provider off as failing.
ANSWERED_FLOOR = 0.2
#: A provider's normal is read over this span, ending :data:`BASELINE_LAG` ago so an episode in progress does not
#: become its own baseline.
BASELINE_SPAN = timedelta(days=7)
BASELINE_LAG = timedelta(days=1)
BASELINE_MIN_ATTEMPTS = 50
BASELINE_REFRESH = timedelta(hours=1)
#: Degraded when the answered share falls below this fraction of its normal.
BASELINE_DROP = 0.5
#: Degraded when the empty share rises by this much over its normal (and at least doubles).
EMPTY_RISE = 0.3
#: The first backoff; each consecutive one doubles, up to :data:`BACKOFF_CAP`.
BACKOFF_BASE = timedelta(minutes=15)
BACKOFF_CAP = timedelta(hours=24)
_MAX_LEVEL = 16
#: A provider healthy this long starts its next backoff from :data:`BACKOFF_BASE` again.
LEVEL_RESET_AFTER = timedelta(hours=24)
#: One background probe per provider this often, while it is probing.
PROBE_INTERVAL_SECONDS = 120
#: Probe calls needed before probing decides anything.
PROBE_SAMPLE = 3
#: Share of probe calls that must be answered, with no refusal, to call it recovered.
PROBE_PASS_SHARE = 0.5
#: Live calls let through per provider per window while it is backed off or probing.
LIVE_TRICKLE_CALLS = 3
LIVE_TRICKLE_WINDOW_SECONDS = 300
#: How long a provider must stay unhealthy before a person is told, so a blip the first probe clears pages nobody.
ALERT_AFTER = timedelta(minutes=30)
ALERT_REMIND_AFTER = timedelta(hours=24)
#: When at least this share of the providers called in the last hour - and at least
#: :data:`NETWORK_DOWN_MIN_PROVIDERS` of them - are failing at once, the digest says this deployment's own network is
#: the likelier cause. REData's endpoints are counted apart (:data:`REDATA_PREFIX`), since REData being down fails
#: all of them at once without saying anything about the network.
NETWORK_DOWN_SHARE = 0.5
NETWORK_DOWN_MIN_PROVIDERS = 5
REDATA_PREFIX = "redata_"
REDATA_DOWN_MIN_PROVIDERS = 3

#: HTTP statuses that mean the source said no.
REFUSED_STATUSES = (401, 403, 429)
#: HTTP statuses that mean the source answered, with nothing.
EMPTY_STATUSES = (404, 410)

_SNAPSHOT_KEY = "provider-health:snapshot:v1"
#: Longer than several evaluations, so a slow or failed run never drops a backoff. Entries carry their own expiry,
#: so a stale snapshot only ever errs towards probing.
_SNAPSHOT_TTL_SECONDS = 3600
#: How long one process reuses the snapshot before reading the cache again.
_SNAPSHOT_MEMO_SECONDS = 15.0
_EVALUATION_LOCK_KEY = "urbanlens:provider-health:evaluate-lock"
EVALUATION_LOCK_SECONDS = 240


class Outcome(StrEnum):
    """What the source did with one call, as far as its ``ApiCallLog`` row says."""

    OK = "ok"
    EMPTY = "empty"
    REFUSED = "refused"
    FAILED = "failed"


#: An ``ApiCallLog`` row's :class:`Outcome`. A status decides it when there is one; a call with none either answered
#: (an SDK call through ``api_call_slot``) or never got a response (a timeout or failed connection).
OUTCOME_EXPRESSION = Case(
    When(status_code__in=REFUSED_STATUSES, then=Value(Outcome.REFUSED.value)),
    When(status_code__in=EMPTY_STATUSES, then=Value(Outcome.EMPTY.value)),
    When(status_code__gte=400, then=Value(Outcome.FAILED.value)),
    When(success=True, then=Value(Outcome.OK.value)),
    default=Value(Outcome.FAILED.value),
    output_field=CharField(),
)


# ---------------------------------------------------------------------------
# Judging a window of calls
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Tally:
    """What one provider's calls did over a window."""

    ok: int = 0
    empty: int = 0
    refused: int = 0
    failed: int = 0
    failure_statuses: Counter[int] = field(default_factory=Counter)

    def add(self, outcome: str, count: int, status_code: int | None = None) -> None:
        """Count ``count`` calls with this outcome.

        Args:
            outcome: An :class:`Outcome` value; anything else counts nothing.
            count: How many calls.
            status_code: Their HTTP status, when they had one.
        """
        if outcome == Outcome.OK:
            self.ok += count
        elif outcome == Outcome.EMPTY:
            self.empty += count
        elif outcome == Outcome.REFUSED:
            self.refused += count
        elif outcome == Outcome.FAILED:
            self.failed += count
        else:
            return
        if outcome in (Outcome.REFUSED, Outcome.FAILED) and status_code:
            self.failure_statuses[status_code] += count

    @property
    def attempts(self) -> int:
        """Calls that reached the source."""
        return self.ok + self.empty + self.refused + self.failed

    @property
    def answered(self) -> int:
        """Calls the source answered, with something or with nothing."""
        return self.ok + self.empty

    def share(self, count: int) -> float:
        """``count`` as a share of :attr:`attempts`."""
        return count / self.attempts if self.attempts else 0.0

    @property
    def failure_status(self) -> int | None:
        """The commonest HTTP status among the refusals and failures."""
        common = self.failure_statuses.most_common(1)
        return common[0][0] if common else None


@dataclass(frozen=True, slots=True)
class Baseline:
    """A provider's normal, over the week before the last day."""

    attempts: int
    answered_share: float | None
    empty_share: float | None

    @property
    def usable(self) -> bool:
        """Whether there were enough calls to call it a normal."""
        return self.attempts >= BASELINE_MIN_ATTEMPTS and self.answered_share is not None and self.empty_share is not None


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a window says about a provider."""

    state: str
    cause: str = BackoffCause.NONE


def judge(tally: Tally, baseline: Baseline | None = None) -> Verdict | None:
    """What a window of calls says about a provider, or ``None`` when it holds too few to say.

    The absolute rules need no baseline on purpose: a provider that has answered nothing all week would otherwise
    have that week as its normal.

    Args:
        tally: The window's calls.
        baseline: The provider's normal, when one has been read.

    Returns:
        The verdict, or None below :data:`MIN_SAMPLE` calls.
    """
    attempts = tally.attempts
    if attempts < MIN_SAMPLE:
        return None
    if tally.refused >= MIN_REFUSALS and tally.refused >= REFUSAL_SHARE * attempts:
        return Verdict(ProviderState.BACKED_OFF, BackoffCause.REFUSED)
    if tally.answered <= ANSWERED_FLOOR * attempts:
        return Verdict(ProviderState.BACKED_OFF, BackoffCause.FAILING)
    if baseline is not None and baseline.usable:
        answered_share = tally.share(tally.answered)
        empty_share = tally.share(tally.empty)
        if baseline.answered_share is not None and answered_share < baseline.answered_share * BASELINE_DROP:
            return Verdict(ProviderState.DEGRADED, BackoffCause.BELOW_BASELINE)
        if baseline.empty_share is not None and empty_share >= baseline.empty_share + EMPTY_RISE and empty_share >= 2 * baseline.empty_share:
            return Verdict(ProviderState.DEGRADED, BackoffCause.BELOW_BASELINE)
    return Verdict(ProviderState.HEALTHY)


def backoff_duration(level: int) -> timedelta:
    """How long the ``level``-th consecutive backoff lasts: doubling from 15 minutes, capped at a day.

    Args:
        level: 1 for the first backoff in a row.

    Returns:
        The backoff's length.
    """
    exponent = max(min(level, _MAX_LEVEL) - 1, 0)
    return min(BACKOFF_BASE * (2**exponent), BACKOFF_CAP)


def _span(delta: timedelta) -> str:
    minutes = round(delta.total_seconds() / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {rest} min" if rest else f"{hours} h"
    return f"{hours // 24} days"


def describe(tally: Tally, window: str, *, baseline: Baseline | None = None) -> str:
    """One sentence naming the counts behind a verdict, for the site admin and the alert.

    Args:
        tally: The window's calls.
        window: How the window reads in a sentence ("in the last hour").
        baseline: The provider's normal, to quote it beside the counts.

    Returns:
        The sentence.
    """
    status = f", mostly HTTP {tally.failure_status}" if tally.failure_status else ""
    parts = [f"{tally.answered} of {tally.attempts} calls answered {window}"]
    failures = [(tally.refused, "refused"), (tally.failed, "failed")]
    detail = ", ".join(f"{count} {label}" for count, label in failures if count)
    if detail:
        parts.append(f"{detail}{status}")
    if tally.empty:
        parts.append(f"{tally.empty} answered with nothing")
    sentence = "; ".join(parts)
    if baseline is not None and baseline.usable and baseline.answered_share is not None and baseline.empty_share is not None:
        sentence += f" (normally {baseline.answered_share:.0%} answered, {baseline.empty_share:.0%} empty)"
    return sentence + "."


# ---------------------------------------------------------------------------
# Reading the call log
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Count:
    provider: str
    bucket: datetime | None
    outcome: str
    status_code: int | None
    count: int


def _known_services() -> list[str]:
    from urbanlens.dashboard.models.api_rate_limit import ApiRateLimit
    from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

    return sorted(set(all_service_defaults()) | set(ApiRateLimit.objects.values_list("service", flat=True)))


def _bucket_expression() -> Func:
    return Func(F("created"), function="", template=f"to_timestamp(floor(extract(epoch from %(expressions)s) / {BUCKET_SECONDS}) * {BUCKET_SECONDS})", output_field=DateTimeField())


def _read_counts(since: datetime, until: datetime | None = None, *, bucketed: bool = True) -> list[_Count]:
    """Calls that reached a source since ``since``, by provider, outcome, status and bucket.

    A call refused here before it was sent (rate limited, disabled, geo-filtered) says nothing about the source, and a
    reservation still waiting for its response has no outcome yet; both are left out.

    Args:
        since: The start of the window.
        until: The end of the window; None for now.
        bucketed: Whether to split the counts into :data:`BUCKET_SECONDS` buckets.

    Returns:
        One count per provider, outcome, status and bucket.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    rows = ApiCallLog.objects.filter(service__in=_known_services(), created__gte=since, was_rate_limited=False, was_geo_filtered=False, was_service_disabled=False, was_rejected_input=False).exclude(
        success=True, response_ms__isnull=True, status_code__isnull=True
    )
    if until is not None:
        rows = rows.filter(created__lt=until)
    annotated = rows.annotate(derived=OUTCOME_EXPRESSION)
    fields = ["service", "derived", "status_code"]
    if bucketed:
        annotated = annotated.annotate(bucket=_bucket_expression())
        fields.append("bucket")
    return [_Count(row["service"], row.get("bucket"), row["derived"], row["status_code"], row["n"]) for row in annotated.values(*fields).annotate(n=Count("id")).order_by()]


def _tally(counts: Iterable[_Count], *, since: datetime | None = None) -> Tally:
    """Sum counts whose bucket starts at or after ``since``; a bucket straddling it is left out."""
    tally = Tally()
    for item in counts:
        if since is not None and item.bucket is not None and item.bucket < since:
            continue
        tally.add(item.outcome, item.count, item.status_code)
    return tally


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvaluationReport:
    """What one :func:`evaluate_provider_health` run did."""

    evaluated: int = 0
    transitions: list[str] = field(default_factory=list)
    alerted: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    skipped: bool = False


@dataclass(slots=True)
class _Step:
    """What happened to one provider in one run."""

    transition: str = ""
    #: The digest's line for a provider a person was told about and that is now healthy.
    recovered: str = ""
    #: Called in the last hour, or backed off or probing because it was failing.
    active_in_hour: bool = False
    #: Backed off or probing because it was failing, or at least :data:`MIN_SAMPLE` calls in the last hour that mostly
    #: went unanswered other than by a refusal. A dead network times out; it does not answer 429.
    failing_in_hour: bool = False


def _refresh_baselines(rows: Iterable[ProviderHealth], now: datetime) -> None:
    """Recompute every provider's normal, once an hour."""
    from urbanlens.dashboard.models.provider_health import ProviderHealth

    newest = ProviderHealth.objects.aggregate(newest=Max("baseline_computed_at"))["newest"]
    if newest is not None and newest > now - BASELINE_REFRESH:
        return
    grouped: dict[str, list[_Count]] = defaultdict(list)
    for item in _read_counts(now - BASELINE_LAG - BASELINE_SPAN, now - BASELINE_LAG, bucketed=False):
        grouped[item.provider].append(item)
    for row in rows:
        tally = _tally(grouped.get(row.provider, ()))
        row.baseline_attempts = tally.attempts
        row.baseline_answered_share = tally.share(tally.answered) if tally.attempts else None
        row.baseline_empty_share = tally.share(tally.empty) if tally.attempts else None
        row.baseline_computed_at = now


def _baseline_of(row: ProviderHealth) -> Baseline:
    return Baseline(row.baseline_attempts, row.baseline_answered_share, row.baseline_empty_share)


def _record_window(row: ProviderHealth, tally: Tally, minutes: int) -> None:
    row.window_minutes = minutes
    row.attempts = tally.attempts
    row.answered = tally.answered
    row.empty = tally.empty
    row.refused = tally.refused
    row.failed = tally.failed
    row.failure_status = tally.failure_status


def _trip(row: ProviderHealth, cause: str, reason: str, now: datetime) -> str:
    if row.state == ProviderState.DEGRADED:
        # Worse than what a person was told about: tell them again rather than wait a day.
        row.alerted_at = None
    row.level = min(row.level + 1, _MAX_LEVEL)
    duration = backoff_duration(row.level)
    row.state = ProviderState.BACKED_OFF
    row.cause = cause
    row.reason = reason
    row.state_since = now
    row.backed_off_until = now + duration
    row.episode_started_at = row.episode_started_at or now
    logger.warning("provider_health: backing off %s for %s (level %d, %s): %s", row.provider, _span(duration), row.level, cause, reason)
    return f"{row.provider}: backed off for {_span(duration)}"


def _recover(row: ProviderHealth, reason: str, now: datetime, step: _Step, *, quiet: bool = False) -> str:
    if row.episode_started_at is not None and row.alerted_at is not None and row.alerted_at >= row.episode_started_at:
        span = _span(now - row.episode_started_at)
        step.recovered = f"- {row.provider} cleared after {span}: called too rarely to judge any more." if quiet else f"- {row.provider} recovered after {span}."
    row.counted_from = row.state_since if row.state == ProviderState.PROBING else now
    row.state = ProviderState.HEALTHY
    row.cause = BackoffCause.NONE
    row.reason = reason
    row.state_since = now
    row.backed_off_until = None
    row.episode_started_at = None
    logger.warning("provider_health: %s %s: %s", row.provider, "cleared" if quiet else "recovered", reason)
    return f"{row.provider}: {'cleared' if quiet else 'recovered'}"


def _cause_of(tally: Tally) -> str:
    return BackoffCause.REFUSED if tally.refused >= MIN_REFUSALS and tally.refused >= REFUSAL_SHARE * tally.attempts else BackoffCause.FAILING


def _note_activity(row: ProviderHealth, counts: list[_Count]) -> None:
    seen = [item.bucket for item in counts if item.bucket is not None]
    if seen:
        row.last_attempt_at = max(filter(None, (row.last_attempt_at, max(seen))))
    answered = [item.bucket for item in counts if item.bucket is not None and item.outcome in (Outcome.OK, Outcome.EMPTY)]
    if answered:
        row.last_ok_at = max(filter(None, (row.last_ok_at, max(answered))))


def _note_failing(row: ProviderHealth, hour: Tally, step: _Step) -> None:
    """What the network-down and REData-down subjects read about this provider, after its step."""
    held_for_failing = row.state in (ProviderState.BACKED_OFF, ProviderState.PROBING) and row.cause == BackoffCause.FAILING
    step.active_in_hour = hour.attempts > 0 or held_for_failing
    step.failing_in_hour = held_for_failing or (hour.attempts >= MIN_SAMPLE and hour.answered <= ANSWERED_FLOOR * hour.attempts and hour.refused < REFUSAL_SHARE * hour.attempts)


def _step(row: ProviderHealth, counts: list[_Count], now: datetime) -> _Step:
    """Move one provider through its states on this run's counts.

    An unhealthy provider that stops being called is cleared once there is too little left to judge it on - a day of
    probing without :data:`PROBE_SAMPLE` calls, or a degraded one with under :data:`MIN_SAMPLE` calls in the day - so a
    retired source is not reminded about forever. Calls that come back are judged afresh.
    """
    step = _Step()
    _note_activity(row, counts)

    if row.state == ProviderState.HEALTHY and row.level and row.state_since <= now - LEVEL_RESET_AFTER:
        row.level = 0

    if row.state == ProviderState.BACKED_OFF:
        if row.backed_off_until is None or now >= row.backed_off_until:
            row.state = ProviderState.PROBING
            row.state_since = now
            logger.info("provider_health: %s backoff over - probing", row.provider)
            step.transition = f"{row.provider}: probing"
        return step

    if row.state == ProviderState.PROBING:
        probe = _tally(counts, since=row.state_since)
        if probe.attempts >= PROBE_SAMPLE:
            _record_window(row, probe, round((now - row.state_since).total_seconds() / 60))
            if probe.refused == 0 and probe.answered >= PROBE_PASS_SHARE * probe.attempts:
                step.transition = _recover(row, f"Probe answered: {describe(probe, 'since probing began')}", now, step)
            else:
                step.transition = _trip(row, _cause_of(probe), f"Probe failed: {describe(probe, 'since probing began')}", now)
        elif now - row.state_since >= LONG_WINDOW:
            step.transition = _recover(row, f"Not called enough to probe in the {_span(now - row.state_since)} since probing began ({probe.attempts} calls).", now, step, quiet=True)
        return step

    floor = row.counted_from
    short = _tally(counts, since=max(filter(None, (now - SHORT_WINDOW, floor))))
    if short.attempts >= MIN_SAMPLE:
        window, minutes, label = short, 60, "in the last hour"
    else:
        window, minutes, label = _tally(counts, since=max(filter(None, (now - LONG_WINDOW, floor)))), 1440, "in the last 24 hours"
    verdict = judge(window, _baseline_of(row))
    if verdict is None:
        if row.state == ProviderState.DEGRADED:
            step.transition = _recover(row, f"Too few calls in the last 24 hours to judge ({window.attempts}).", now, step, quiet=True)
        return step
    _record_window(row, window, minutes)
    reason = describe(window, label, baseline=_baseline_of(row) if verdict.cause == BackoffCause.BELOW_BASELINE else None)
    if verdict.state == ProviderState.BACKED_OFF:
        step.transition = _trip(row, verdict.cause, reason, now)
    elif verdict.state == ProviderState.DEGRADED:
        row.reason = reason
        row.cause = verdict.cause
        if row.state == ProviderState.HEALTHY:
            row.state = ProviderState.DEGRADED
            row.state_since = now
            row.episode_started_at = now
            logger.warning("provider_health: %s is degraded: %s", row.provider, reason)
            step.transition = f"{row.provider}: degraded"
    elif row.state == ProviderState.DEGRADED:
        step.transition = _recover(row, reason, now, step)
    else:
        row.reason = reason
    return step


def evaluate_provider_health(*, now: datetime | None = None) -> EvaluationReport:
    """Judge every provider on the last day of calls, move each one's state, publish the gate's snapshot, and alert.

    Args:
        now: The time to evaluate at; the current time when omitted.

    Returns:
        What the run did. ``skipped`` when another run held the lock.
    """
    from urbanlens.dashboard.models.provider_health import ProviderHealth

    report = EvaluationReport()
    with beat_lock(_EVALUATION_LOCK_KEY, EVALUATION_LOCK_SECONDS) as acquired:
        if not acquired:
            report.skipped = True
            return report
        now = now or timezone.now()
        grouped: dict[str, list[_Count]] = defaultdict(list)
        for item in _read_counts(now - LONG_WINDOW):
            grouped[item.provider].append(item)
        rows = {row.provider: row for row in ProviderHealth.objects.filter(Q(provider__in=list(grouped)) | ~Q(state=ProviderState.HEALTHY))}
        for provider in grouped.keys() - rows.keys():
            rows[provider] = ProviderHealth(provider=provider, state_since=now)
        _refresh_baselines(rows.values(), now)

        steps: dict[str, _Step] = {}
        for provider, row in sorted(rows.items()):
            counts = grouped.get(provider, [])
            steps[provider] = _step(row, counts, now)
            _note_failing(row, _tally(counts, since=now - SHORT_WINDOW), steps[provider])
            row.last_evaluated_at = now
            row.save()
            if steps[provider].transition:
                report.transitions.append(steps[provider].transition)
        report.evaluated = len(rows)

        # Published before alerting, so a failed delivery can never leave the gate without a new backoff.
        write_snapshot()
        try:
            _alert(rows, steps, now, report)
        except Exception:
            logger.exception("provider_health: could not deliver the alert digest - it is retried on the next run")
    return report


# ---------------------------------------------------------------------------
# Telling a person
# ---------------------------------------------------------------------------


def alerts_enabled() -> bool:
    """Whether a digest is sent: never under ``DEBUG`` or in tests."""
    return not (django_settings.DEBUG or getattr(django_settings, "TESTING", False))


def _paused_line(row: ProviderHealth) -> str:
    if row.state == ProviderState.BACKED_OFF and row.backed_off_until is not None:
        return f"Background calls paused until {row.backed_off_until:%H:%M}Z, live calls limited to {LIVE_TRICKLE_CALLS} per {LIVE_TRICKLE_WINDOW_SECONDS // 60} min."
    if row.state == ProviderState.PROBING:
        return f"Probing: one background call every {PROBE_INTERVAL_SECONDS // 60} min."
    return "Nothing paused."


def _alert_line(row: ProviderHealth, now: datetime) -> str:
    last_ok = f"last answer {_span(now - row.last_ok_at)} ago" if row.last_ok_at else "no answer in the last day"
    since = _span(now - row.episode_started_at) if row.episode_started_at else "?"
    return f"- {row.provider} - {row.get_state_display().lower()} ({row.get_cause_display().lower()}) for {since}, {last_ok}. {row.reason} {_paused_line(row)}"


def _title(due: list[ProviderHealth], recovered: list[str], steps: dict[str, _Step]) -> str:
    """The digest's subject: the likeliest single cause when the failures share one, else a count."""
    others = [step for provider, step in steps.items() if step.active_in_hour and not provider.startswith(REDATA_PREFIX)]
    failing_others = [step for step in others if step.failing_in_hour]
    if len(others) >= NETWORK_DOWN_MIN_PROVIDERS and len(failing_others) >= NETWORK_DOWN_SHARE * len(others):
        return f"UrbanLens: {len(failing_others)} of {len(others)} providers failing at once - its outbound network may be down"
    redata = [step for provider, step in steps.items() if step.active_in_hour and provider.startswith(REDATA_PREFIX)]
    failing_redata = [step for step in redata if step.failing_in_hour]
    if len(failing_redata) >= REDATA_DOWN_MIN_PROVIDERS and len(failing_redata) >= NETWORK_DOWN_SHARE * len(redata):
        return f"UrbanLens: REData is failing - {len(failing_redata)} of the {len(redata)} REData services called in the last hour"
    if due:
        return f"UrbanLens: {len(due)} provider{'s' if len(due) != 1 else ''} refusing or failing" + (f", {len(recovered)} recovered" if recovered else "")
    return f"UrbanLens: {len(recovered)} provider{'s' if len(recovered) != 1 else ''} recovered"


def _alert(rows: dict[str, ProviderHealth], steps: dict[str, _Step], now: datetime, report: EvaluationReport) -> None:
    due = [
        row
        for row in rows.values()
        if row.state != ProviderState.HEALTHY and row.episode_started_at is not None and now - row.episode_started_at >= ALERT_AFTER and (row.alerted_at is None or row.alerted_at < row.episode_started_at or now - row.alerted_at >= ALERT_REMIND_AFTER)
    ]
    recovered = [steps[provider].recovered for provider in sorted(steps) if steps[provider].recovered]
    if not due and not recovered:
        return

    lines = [_alert_line(row, now) for row in sorted(due, key=lambda row: row.provider)]
    lines += recovered
    lines.append("Details: Site admin > API Rate Limits > Provider health.")
    title = _title(due, recovered, steps)
    message = "\n".join(lines)
    logger.warning("provider_health: %s\n%s", title, message)
    deliver_alert(title, message)

    for row in due:
        row.alerted_at = now
        row.save(update_fields=["alerted_at", "updated"])
        report.alerted.append(row.provider)
    report.recovered.extend(provider for provider in sorted(steps) if steps[provider].recovered)


def deliver_alert(title: str, message: str) -> None:
    """Send one digest to every channel routed for the ``provider_health`` event, unless alerts are off.

    Args:
        title: The digest's subject.
        message: Its body.
    """
    from urbanlens.dashboard.services.notifications.notifications import NotificationEvent, notify

    if not alerts_enabled():
        return
    notify(NotificationEvent.PROVIDER_HEALTH, title, message)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    state: str
    cause: str
    until: float | None


@dataclass(slots=True)
class _Memo:
    """This process's copy of the snapshot, and when it must be read again."""

    expires: float = 0.0
    entries: dict[str, _Entry] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def forget(self) -> None:
        with self.lock:
            self.expires = 0.0
            self.entries = {}


_memo = _Memo()


def write_snapshot() -> None:
    """Publish every unhealthy provider for the gate. Called by the evaluator after each run."""
    from urbanlens.dashboard.models.provider_health import ProviderHealth

    try:
        payload = {row.provider: [row.state, row.cause, row.backed_off_until.timestamp() if row.backed_off_until else None] for row in ProviderHealth.objects.unhealthy()}
    except DatabaseError:
        logger.exception("provider_health: could not read provider states for the gate")
        return
    try:
        cache.set(_SNAPSHOT_KEY, payload, _SNAPSHOT_TTL_SECONDS)
    except _CACHE_ERRORS:
        logger.exception("provider_health: could not publish provider states for the gate")
    _memo.forget()


def forget_snapshot() -> None:
    """Drop this process's copy of the snapshot, so the next call reads the cache."""
    _memo.forget()


def _parse_snapshot(payload: Any) -> dict[str, _Entry]:
    if not isinstance(payload, dict):
        return {}
    return {str(provider): _Entry(str(value[0]), str(value[1]), float(value[2]) if value[2] is not None else None) for provider, value in payload.items() if isinstance(value, list | tuple) and len(value) == 3}


def _snapshot() -> dict[str, _Entry]:
    now = time.monotonic()
    if _memo.expires > now:
        return _memo.entries
    try:
        payload = cache.get(_SNAPSHOT_KEY)
    except _CACHE_ERRORS:
        logger.exception("provider_health: could not read provider states - allowing calls")
        payload = None
    entries = _parse_snapshot(payload)
    with _memo.lock:
        _memo.expires, _memo.entries = now + _SNAPSHOT_MEMO_SECONDS, entries
    return entries


def _take(key: str, *, limit: int, window_seconds: int) -> bool:
    """One of ``limit`` slots in the current ``window_seconds``; admits on a cache error."""
    counter = make_cache_key("provider-health:slot", key, int(time.time() // window_seconds))
    try:
        cache.add(counter, 0, window_seconds * 2)
        return int(cache.incr(counter)) <= limit
    except _CACHE_ERRORS:
        return True


def _count_suppressed(provider: str) -> None:
    counter = make_cache_key("provider-health:suppressed", provider, timezone.now().date().isoformat())
    try:
        cache.add(counter, 0, 2 * 86_400)
        cache.incr(counter)
    except _CACHE_ERRORS:
        return


def suppressed_today(provider: str) -> int:
    """How many calls the gate refused for ``provider`` today (UTC), for the site admin.

    Args:
        provider: The provider's service key.

    Returns:
        The count; 0 when the cache cannot say.
    """
    counter = make_cache_key("provider-health:suppressed", provider, timezone.now().date().isoformat())
    try:
        return int(cache.get(counter) or 0)
    except (*_CACHE_ERRORS, TypeError):
        return 0


@dataclass(frozen=True, slots=True)
class Refusal:
    """Why the gate said no."""

    provider: str
    retry_after: int
    reason: str


def refusal_for(service: str, *, background: bool | None = None) -> Refusal | None:
    """Why a call to ``service`` must not be made now, or ``None`` to go ahead.

    A ``None`` uses up a live or probe slot where the provider needed one, so call this only for a call that will be
    made. Admits on any cache failure.

    Args:
        service: The rate-limiter service key.
        background: Whether the call is background work; read from ``background_work`` when omitted.

    Returns:
        The refusal, or None.
    """
    entry = _snapshot().get(service)
    if entry is None or entry.state in (ProviderState.HEALTHY, ProviderState.DEGRADED):
        return None
    now = time.time()
    backed_off = entry.state == ProviderState.BACKED_OFF and entry.until is not None and now < entry.until
    remaining = max(int((entry.until or now) - now), 1) if backed_off else PROBE_INTERVAL_SECONDS
    refusal: Refusal | None = None
    if not (is_background() if background is None else background):
        if not _take(f"live:{service}", limit=LIVE_TRICKLE_CALLS, window_seconds=LIVE_TRICKLE_WINDOW_SECONDS):
            refusal = Refusal(service, min(remaining, LIVE_TRICKLE_WINDOW_SECONDS), f"{service} is {entry.state.replace('_', ' ')} and its live calls are spent for now")
    elif backed_off or not _take(f"probe:{service}", limit=1, window_seconds=PROBE_INTERVAL_SECONDS):
        refusal = Refusal(service, remaining, f"{service} is {entry.state.replace('_', ' ')} ({entry.cause or 'unknown'})")
    if refusal is not None:
        _count_suppressed(service)
    return refusal


def check_admission(service: str, *, endpoint: str = "") -> None:
    """Refuse a call ``refusal_for`` says no to, the way a tripped ``upstream_breaker`` does.

    The refusal is logged as rate limited and recorded as unanswered, so a caller building a "nothing here" answer
    inside ``outages_observed`` sees it and does not keep that answer.

    Args:
        service: The rate-limiter service key.
        endpoint: What was about to be called, for the ``ApiCallLog`` row.

    Raises:
        UpstreamThrottledError: The provider is backed off, or this call has no live or probe slot.
    """
    from urbanlens.dashboard.services.core.outages import record_unanswered
    from urbanlens.dashboard.services.core.rate_limiter import UpstreamThrottledError, log_api_call

    if (refusal := refusal_for(service)) is None:
        return
    logger.debug("provider_health: refused a call to %s: %s", service, refusal.reason)
    log_api_call(service, success=False, endpoint=endpoint, was_rate_limited=True)
    record_unanswered(service)
    raise UpstreamThrottledError(service, retry_after=refusal.retry_after)


__all__ = [
    "Baseline",
    "EvaluationReport",
    "Outcome",
    "Refusal",
    "Tally",
    "Verdict",
    "backoff_duration",
    "check_admission",
    "describe",
    "evaluate_provider_health",
    "forget_snapshot",
    "judge",
    "refusal_for",
    "suppressed_today",
    "write_snapshot",
]
