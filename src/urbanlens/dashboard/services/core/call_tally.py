"""Calls counted in the shared cache instead of written to the ledger one row each, and rolled up into it every minute.

For a service whose ``ServiceDefaults.ledger`` is :attr:`~urbanlens.dashboard.services.core.rate_limiter.CallLedger.TALLIED`.
It is held to the same ``ApiRateLimit`` row, the same environment share (D26) and the same refusals as any other
service, but the check is a windowed counter in the default cache (``services.core.counters``) rather than a
``SELECT ... FOR UPDATE`` on that row and a ``COUNT`` of the ledger, and each call's outcome is added to a per-service
tally there rather than inserted as a row. :func:`roll_up` (beat, every minute) turns the tally into ``ApiCallLog``
rows that each stand for ``calls`` calls, so the API-limits page, the costs page and provider health keep reading
one ledger. The row's limits reach a call the same way: published to the cache by the roll-up and on every save of
the row, so a call reads no row either. Nothing a call does, from admission to record, waits on the database.

The basemap tile proxy is what this is for: one call per uncached tile, a cold viewport's worth at once. Locked and
counted per call, a burst of tiles queues on the one row lock while each holds a pooled database connection.

What it gives up, against the per-call ledger:

- The windows are fixed - the UTC minute, the UTC day, a thirty-day period, the UTC month - where the ledger's
  minute and thirty days roll, so a burst straddling a boundary can reach twice a limit within one window's length.
  The counters start empty, so calls made before a service became tallied are not counted against them.
- An edit to the row reaches a process's calls within :data:`LIMITS_MEMO_SECONDS`.
- A counter store that cannot answer refuses the call: nothing could count it, and a limit that guards someone
  else's budget is not dropped for an outage (``counters.Outage.REFUSE``).
- The ledger is a minute behind, and an outcome the store cannot take waits in its process for the next one it
  can. A process that dies holding some loses them; the budget was counted when they were admitted.
- A rolled-up row is nobody's: which viewer happened to miss the cache first is not their consumption.
"""

from __future__ import annotations

import contextlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import json
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from django.core.cache import cache
from django.db import DatabaseError, transaction

from urbanlens.dashboard.services.core import counters
from urbanlens.dashboard.services.core.counters import CounterUnavailableError, Outage

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal

logger = logging.getLogger(__name__)

#: How long one process trusts the limits it last read before reading the published copy again.
LIMITS_MEMO_SECONDS = 15.0
#: How long a published copy of the limits lasts unrefreshed; :func:`roll_up` refreshes it every minute.
LIMITS_CACHE_SECONDS = 600
#: How long a tally outlives its last addition: long enough to ride out a beat outage of a few hours.
TALLY_TTL_SECONDS = 6 * 3600
_MINUTE = 60
_DAY = 86_400
#: Anything the plain cache API can raise when it cannot answer.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)


class Tallied(StrEnum):
    """What became of one tallied call, and so which ``ApiCallLog`` flags its rolled-up row carries."""

    MADE = "made"
    RATE_LIMITED = "rate_limited"
    SERVICE_DISABLED = "service_disabled"
    GEO_FILTERED = "geo_filtered"
    REJECTED_INPUT = "rejected_input"


@dataclass(frozen=True, slots=True)
class Limits:
    """The parts of an ``ApiRateLimit`` row a tallied call is checked against."""

    enabled: bool
    calls_per_minute: int | None
    calls_per_day: int | None
    calls_per_30_days: int | None
    min_interval_seconds: float | None

    @classmethod
    def of(cls, row: Any) -> Limits:
        """The limits an ``ApiRateLimit`` row holds."""
        return cls(
            enabled=bool(row.enabled),
            calls_per_minute=row.calls_per_minute,
            calls_per_day=row.calls_per_day,
            calls_per_30_days=row.calls_per_30_days,
            min_interval_seconds=row.min_interval_seconds,
        )


_limits: dict[str, tuple[float, Limits]] = {}
_limits_lock = threading.Lock()


def _limits_key(service: str) -> str:
    return f"ul:calls:{service}:limits"


def _remember(service: str, limits: Limits) -> None:
    with _limits_lock:
        _limits[service] = (time.monotonic(), limits)


def forget_limits() -> None:
    """Drop every limit this process remembers, and the shared copies. For tests."""
    with _limits_lock:
        services = list(_limits)
        _limits.clear()
    for service in services:
        with contextlib.suppress(*_CACHE_ERRORS):
            cache.delete(_limits_key(service))


def publish_limits(service: str, row: Any = None) -> Limits:
    """Share *service*'s row through the default cache, where every process's next call reads it.

    Called by :func:`roll_up` every minute and whenever the row is saved (``models.api_rate_limit.signals``), so a
    call itself need never read the row.

    Args:
        service: The service key.
        row: The ``ApiRateLimit`` row, when the caller holds it; read otherwise.

    Returns:
        Its limits.

    Raises:
        DatabaseError: The row had to be read and could not be.
    """
    from urbanlens.dashboard.services.core.rate_limiter import get_limit_config

    limits = Limits.of(row if row is not None else get_limit_config(service))
    with contextlib.suppress(*_CACHE_ERRORS):
        cache.set(_limits_key(service), asdict(limits), LIMITS_CACHE_SECONDS)
    _remember(service, limits)
    return limits


def limits_for(service: str) -> Limits:
    """*service*'s limits: as this process last saw them, else as last published, else from its row.

    The row is read only when nothing has published it - a fresh store before the first roll-up - and the
    reader publishes what it read, so that is one read for the fleet rather than one per call.

    Args:
        service: The service key.

    Returns:
        Its limits.

    Raises:
        DatabaseError: The row had to be read and could not be.
    """
    remembered = _limits.get(service)
    if remembered is not None and time.monotonic() - remembered[0] < LIMITS_MEMO_SECONDS:
        return remembered[1]
    published = cache.get(_limits_key(service))
    if isinstance(published, dict):
        try:
            limits = Limits(**published)
        except TypeError:
            logger.warning("Ignoring unreadable published limits for %s", service)
        else:
            _remember(service, limits)
            return limits
    return publish_limits(service)


def _now() -> float:
    """Wall-clock seconds; one place for a test to move the windows."""
    return time.time()


def _interval_key(service: str) -> str:
    return f"ul:calls:{service}:interval"


def tally_key(service: str) -> str:
    """Where *service*'s outcomes wait for :func:`roll_up`."""
    return f"ul:calls:{service}:tally"


def _windows(service: str, limits: Limits, now: float) -> tuple[tuple[str, int | None, int], ...]:
    """``(counter key, limit at this deployment's share, ttl)`` for each budget window *now* falls in.

    Every one is fixed, keyed by the period it counts: the UTC minute, the UTC day, the thirty-day period since the
    epoch and the UTC calendar month. The ledger's minute and thirty days roll; its day and month are these same
    calendar periods.
    """
    from urbanlens.dashboard.services.core.rate_limiter import _service_share, free_tier_ceiling
    from urbanlens.UrbanLens.egress import scaled_limit

    share = _service_share(service)
    day = int(now // _DAY)
    month = datetime.fromtimestamp(now, tz=UTC).strftime("%Y%m")
    return (
        (f"ul:calls:{service}:minute:{int(now // _MINUTE)}", scaled_limit(limits.calls_per_minute, share), 2 * _MINUTE),
        (f"ul:calls:{service}:day:{day}", scaled_limit(limits.calls_per_day, share), 2 * _DAY),
        (f"ul:calls:{service}:30days:{day // 30}", scaled_limit(limits.calls_per_30_days, share), 31 * _DAY),
        # Already this deployment's share of the vendor's allowance.
        (f"ul:calls:{service}:month:{month}", free_tier_ceiling(service), 32 * _DAY),
    )


@dataclass(slots=True)
class Admission:
    """One call the counters let through: recorded once its outcome is known, or released if it is never made.

    Attributes:
        service: The service key.
        endpoint: What is being called, as the ledger describes it.
        minute: The UTC minute the call was admitted in, which its rolled-up row is dated to.
        counted: The window counters this call was added to.
        interval_us: The minimum-interval bucket's refill time, when the call took a token from it.
    """

    service: str
    endpoint: str
    minute: int
    counted: list[str] = field(default_factory=list)
    interval_us: int | None = None

    def record(self, *, success: bool, status_code: int | None, response_ms: int | None) -> None:
        """Add the call's outcome to the tally.

        Args:
            success: Whether it succeeded.
            status_code: The upstream's status; None when no response arrived.
            response_ms: How long it took.
        """
        record(self.service, Tallied.MADE, endpoint=self.endpoint, success=success, status_code=status_code, response_ms=response_ms, minute=self.minute)

    def release(self) -> None:
        """Give back what admitting a call that was never made took from the windows."""
        for key in self.counted:
            counters.refund(key)
        if self.interval_us is not None:
            counters.return_token(_interval_key(self.service), interval_us=self.interval_us)


def _take(service: str, limits: Limits, windows: tuple[tuple[str, int | None, int], ...], admission: Admission) -> bool:
    """Count one call into the minimum interval and each window.

    Args:
        service: The service key.
        limits: Its limits.
        windows: ``(counter key, limit at this deployment's share, ttl)`` per window.
        admission: Where to note what was taken, so it can be given back.

    Returns:
        Whether every one had room; when one did not, nothing is kept.

    Raises:
        CounterUnavailableError: The store could not count; what was already taken is the caller's to give back.
    """
    if limits.min_interval_seconds:
        interval_us = max(int(limits.min_interval_seconds * 1_000_000), 1)
        if not counters.take_token(_interval_key(service), interval_us=interval_us, burst=1, on_outage=Outage.REFUSE):
            return False
        admission.interval_us = interval_us
    for key, limit, ttl in windows:
        if limit is None:
            continue
        count = counters.hit(key, ttl, on_outage=Outage.REFUSE)
        # Noted before the check, so a refusal gives back its own count too: kept, refusals would push the window
        # past its limit and use up room an admin edit adds partway through it.
        admission.counted.append(key)
        if count > limit:
            admission.release()
            return False
    return True


def admit(service: str, *, endpoint: str = "") -> Admission:
    """Check a call to *service* against its limits on the counters, and count it. Never touches the database once
    the limits are published (:func:`limits_for`).

    Args:
        service: A tallied service's key.
        endpoint: What is being called, as the ledger describes it.

    Returns:
        The admission.

    Raises:
        EnvironmentRefusedError: This environment does not call the service; nothing is recorded.
        RateLimitExceededError: A window is full, or the minimum interval has not passed.
        ServiceDisabledError: The service is switched off.
        RateLimiterUnavailableError: Its limits could not be read, or the counter store cannot count: refused, as
            ``counters.Outage.REFUSE`` refuses a limit guarding someone else's budget.
    """
    from urbanlens.dashboard.services.core.egress import require_egress
    from urbanlens.dashboard.services.core.rate_limiter import RateLimiterUnavailableError, RateLimitExceededError, ServiceDisabledError

    require_egress(service)
    try:
        limits = limits_for(service)
    except DatabaseError as exc:
        logger.exception("Failed to read the limits for %s - refusing the call", service)
        raise RateLimiterUnavailableError(service) from exc

    now = _now()
    minute = int(now // _MINUTE)
    if not limits.enabled:
        record(service, Tallied.SERVICE_DISABLED, endpoint=endpoint, minute=minute)
        raise ServiceDisabledError(service)

    admission = Admission(service=service, endpoint=endpoint, minute=minute)
    try:
        admitted = _take(service, limits, _windows(service, limits, now), admission)
    except CounterUnavailableError as exc:
        admission.release()
        logger.warning("Counter store unavailable; refusing a call to %s rather than making it uncounted", service)
        raise RateLimiterUnavailableError(service) from exc
    if not admitted:
        logger.warning("Rate limit hit for %s", service)
        record(service, Tallied.RATE_LIMITED, endpoint=endpoint, minute=minute)
        raise RateLimitExceededError(service)
    return admission


def _field(kind: str, minute: int, outcome: Tallied, status_code: int | None, success: bool, endpoint: str) -> str:
    return json.dumps([kind, minute, outcome.value, status_code, success, endpoint[:500]], separators=(",", ":"))


#: Outcomes this process could not add to the store, carried into its next addition that succeeds.
_pending: dict[str, dict[str, int]] = {}
_pending_lock = threading.Lock()
#: Fields held per service while the store is unreachable; past this, new ones are counted in the log only.
_PENDING_MAX_FIELDS = 5_000


def record(
    service: str,
    outcome: Tallied,
    *,
    endpoint: str = "",
    success: bool = False,
    status_code: int | None = None,
    response_ms: int | None = None,
    minute: int | None = None,
) -> bool:
    """Add one call, or one refusal, to *service*'s tally. Never touches the database.

    An outcome the store cannot take is held in this process and added with its next one that the store does take.

    Args:
        service: The service key.
        outcome: What became of the call.
        endpoint: What was called, as the ledger describes it.
        success: Whether it succeeded.
        status_code: The upstream's status, when there was a response.
        response_ms: How long it took, for a call that was made.
        minute: The UTC minute to date it to; now when omitted.

    Returns:
        Whether it reached the store.
    """
    when = int(_now() // _MINUTE) if minute is None else minute
    increments = {_field("n", when, outcome, status_code, success, endpoint): 1}
    if response_ms is not None:
        increments[_field("ms", when, outcome, status_code, success, endpoint)] = max(int(response_ms), 0)
    with _pending_lock:
        held = _pending.pop(service, None)
    for name, count in (held or {}).items():
        increments[name] = increments.get(name, 0) + count
    try:
        counters.add_to_tally(tally_key(service), increments, TALLY_TTL_SECONDS)
    except CounterUnavailableError:
        with _pending_lock:
            waiting = _pending.setdefault(service, {})
            dropped = 0
            for name, count in increments.items():
                if name in waiting or len(waiting) < _PENDING_MAX_FIELDS:
                    waiting[name] = waiting.get(name, 0) + count
                else:
                    dropped += count
        if dropped:
            logger.warning("Dropped %d tallied outcome(s) for %s while the store is unreachable", dropped, service)
        return False
    return True


def _flags(outcome: Tallied) -> dict[str, bool]:
    return {
        "was_rate_limited": outcome is Tallied.RATE_LIMITED,
        "was_service_disabled": outcome is Tallied.SERVICE_DISABLED,
        "was_geo_filtered": outcome is Tallied.GEO_FILTERED,
        "was_rejected_input": outcome is Tallied.REJECTED_INPUT,
    }


def _cost(service: str, outcome: Tallied, success: bool, calls: int) -> Decimal | None:
    """What *calls* calls cost, as the per-call ledger would have estimated them one at a time."""
    from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

    if outcome is not Tallied.MADE or not success:
        return None
    defaults = all_service_defaults().get(service)
    if defaults is None or defaults.cost_per_call is None:
        return None
    return defaults.cost_per_call * calls


@dataclass(slots=True)
class _Group:
    calls: int = 0
    total_ms: int | None = None


def _groups(service: str, fields: Mapping[str, int]) -> dict[tuple[int, Tallied, int | None, bool, str], _Group]:
    """A drained tally's counts, by minute and outcome."""
    groups: dict[tuple[int, Tallied, int | None, bool, str], _Group] = {}
    for name, value in fields.items():
        try:
            kind, minute, outcome, status_code, success, endpoint = json.loads(name)
            key = (int(minute), Tallied(outcome), None if status_code is None else int(status_code), bool(success), str(endpoint))
            # A minute no row can be dated to would fail every roll-up after it, each putting it back.
            datetime.fromtimestamp(key[0] * _MINUTE, tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            logger.warning("Dropping an unreadable field from %s's call tally: %r", service, name)
            continue
        group = groups.setdefault(key, _Group())
        if kind == "n":
            group.calls += int(value)
        elif kind == "ms":
            group.total_ms = (group.total_ms or 0) + int(value)
    return groups


def _rows(service: str, fields: Mapping[str, int]) -> list[tuple[Any, datetime]]:
    """One unsaved ``ApiCallLog`` per minute and outcome, each with the instant it is to be dated to."""
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    rows: list[tuple[Any, datetime]] = []
    for (minute, outcome, status_code, success, endpoint), group in _groups(service, fields).items():
        if group.calls <= 0:
            continue
        row = ApiCallLog(
            service=service,
            endpoint=endpoint,
            success=success,
            status_code=status_code,
            response_ms=None if group.total_ms is None else round(group.total_ms / group.calls),
            cost_estimate=_cost(service, outcome, success, group.calls),
            calls=group.calls,
            **_flags(outcome),
        )
        rows.append((row, datetime.fromtimestamp(minute * _MINUTE, tz=UTC)))
    return rows


def tallied_services() -> list[str]:
    """Every service whose calls are tallied rather than written one row each."""
    from urbanlens.dashboard.services.core.rate_limiter import CallLedger, all_service_defaults

    return sorted(name for name, defaults in all_service_defaults().items() if defaults.ledger is CallLedger.TALLIED)


def roll_up() -> int:
    """Move every tallied service's counts into ``ApiCallLog``, and republish its limits.

    Each service's tally is taken in one step, so two runs at once split it rather than both writing it, and a call
    tallied while this runs lands in the next run. A tally whose rows cannot be written is put back.

    Returns:
        Rows written.
    """
    from urbanlens.dashboard.models.api_call_log import ApiCallLog

    written = 0
    for service in tallied_services():
        try:
            publish_limits(service)
        except DatabaseError:
            logger.exception("Could not read %s's limits to publish them", service)
        key = tally_key(service)
        try:
            fields = counters.take_tally(key)
        except CounterUnavailableError:
            logger.warning("Could not read %s's call tally; it stays for the next run", service)
            continue
        if not fields:
            continue
        rows: list[tuple[Any, datetime]] = []
        try:
            # Inside the handler: the tally is already out of the store, so building its rows is as much a part of
            # writing it as the insert is.
            rows = _rows(service, fields)
            with transaction.atomic():
                saved = ApiCallLog.objects.bulk_create([row for row, _ in rows])
                by_instant: dict[datetime, list[int]] = {}
                for row, (_, instant) in zip(saved, rows, strict=True):
                    by_instant.setdefault(instant, []).append(row.pk)
                # `created` is auto_now_add, which an insert cannot override.
                for instant, pks in by_instant.items():
                    ApiCallLog.objects.filter(pk__in=pks).update(created=instant)
        except Exception as exc:
            # Anything, not only a database error: a task's soft time limit lands here too, and the tally has
            # already been taken out of the store.
            logger.exception("Could not write %s's call tally to the ledger; putting it back", service)
            try:
                counters.add_to_tally(key, fields, TALLY_TTL_SECONDS)
            except CounterUnavailableError:
                logger.exception("Lost %d tallied call field(s) for %s", len(fields), service)
            if not isinstance(exc, DatabaseError):
                raise
            continue
        written += len(rows)
    return written
