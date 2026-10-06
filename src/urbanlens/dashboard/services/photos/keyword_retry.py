"""Asking a keyword source again about a photo it did not answer for (P323).

When a source does not answer for a photo, ``generate_keywords_for_image`` keeps the photo's keywords from it (P322)
and records an :class:`~urbanlens.dashboard.models.images.keyword.ImageKeywordRetry` row for the (photo, source). The
source might be down, it might have refused the call for now, or the photo's analysis copy might not have been
readable. An answer from the source, through any path, deletes the row. :func:`sweep` (beat ``keyword-retry-sweep``)
asks the source again once the row comes due, the longest-waiting first. A photo with no analysis copy at all is not
recorded: the analysis-copy backfill writes the copy and enqueues keywording itself.

Per run:
    At most :data:`SWEEP_BATCH` photos are asked about, and no call is started after
    :data:`SWEEP_TIME_BUDGET_SECONDS`. Only the source that did not answer is asked, never the photo's other sources.

Per source:
    A source is not asked at all in four cases: this environment does not call it (D26: development makes no hosted
    AI call), its API-limits switch is off, provider health has it backed off, or the sweep's own backoff for it is
    running. A refusal stops the source for the rest of the run, and so do :data:`FAILURES_THAT_STOP_A_SOURCE`
    failures in a row: after one failure the run asks about one more photo when one is due, so one photo the source
    cannot answer does not pass for an outage while others wait. A run in which the source failed and answered nothing
    backs it off for longer (``provider_health.backoff_duration``: 15 minutes, doubling, up to a day), and an answer,
    from a sweep or from an upload, ends that backoff. The sweep runs as background work, so provider health gives it
    no live trickle, and every call still passes the rate limiter and the egress policy at the call itself.

Per photo:
    A failure counts against the photo only when the same run got an answer from that source for another photo: the
    source works, and this photo is what it cannot answer. An outage, when it answers nothing, spends none of a photo's
    :data:`MAX_ATTEMPTS`, and neither does a refusal. An analysis copy that cannot be read counts only when the same
    run read another photo's copy, so a storage outage spends nothing either. Once :data:`MAX_ATTEMPTS` failures count,
    the row is given up (``retry_at`` null) and never swept again. A photo that fails while nothing else waits on its
    source can never be told from an outage, so any row still failing :data:`MAX_WAIT` after its first failure
    (``first_failed_at``) is given up too. A row is dropped when the photo no longer wants that source: it is gone, its
    uploader turned keywords off, the provider no longer runs for it, or it has no analysis copy.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from django.core.cache import cache
from django.db import DatabaseError
from django.utils import timezone

from urbanlens.core.cache_keys import make_cache_key
from urbanlens.dashboard.services.photos.photo_keywords import KeywordOutcome

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.keyword import ImageKeywordRetry
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.services.photos.photo_keywords import PhotoKeywordProvider

logger = logging.getLogger(__name__)

#: The wait before asking again, by how many failures have been held against the photo so far.
RETRY_DELAYS: tuple[timedelta, ...] = (
    timedelta(minutes=30),
    timedelta(hours=2),
    timedelta(hours=8),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
)
#: Failures held against a photo before it is given up; the last comes about 11 days after the first.
MAX_ATTEMPTS = len(RETRY_DELAYS)
#: A row still failing this long after its first failure is given up, held against the photo or not.
MAX_WAIT = timedelta(days=30)
#: Failures in a row that stop a source for the rest of a run: one more photo is asked about after the first.
FAILURES_THAT_STOP_A_SOURCE = 2
#: Photos one run asks a source about, across every source.
SWEEP_BATCH = 20
#: A run starts no call after this long. The task's soft limit is 600 s, and an Ollama call may take 60 s.
SWEEP_TIME_BUDGET_SECONDS = 480
#: What a sweep reports for a row it deleted because the photo no longer wants the source.
DROPPED = "dropped"

#: Due rows read per source each run, so rows dropped on sight cost the rows behind them no more than a run.
_WINDOW_PER_PHOTO = 5

#: Outcomes that count towards :data:`SWEEP_BATCH`: a call, or a read of the analysis copy that failed.
_ASKED = frozenset({KeywordOutcome.ANSWERED, KeywordOutcome.UNANSWERED, KeywordOutcome.NO_INPUT})
#: Outcomes that show the run could read a photo's analysis copy.
_READ_A_COPY = frozenset({KeywordOutcome.ANSWERED, KeywordOutcome.UNANSWERED})
#: Refusals: the source is not asked again in the same run.
_REFUSALS = frozenset({KeywordOutcome.BUSY, KeywordOutcome.OFF})

#: Anything the cache can raise when it cannot answer; ``RuntimeError`` is the test suite's network guard.
_CACHE_ERRORS = (ConnectionError, OSError, RuntimeError, ValueError)
#: Longer than the longest backoff, so a running one is never dropped early.
_SOURCE_STATE_TTL_SECONDS = 2 * 86_400


def _delay(attempts: int) -> timedelta:
    return RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]


# ---------------------------------------------------------------------------
# One (photo, source)
# ---------------------------------------------------------------------------


def note_outcome(image: Image, source: str, outcome: KeywordOutcome) -> None:
    """Record what an upload's keyword run came to, for the sweep.

    Nothing that run saw is held against the photo: it cannot tell a source that is down from one that fails on this
    photo. A refusal for good (``OFF``), a provider that does not run for the photo, and a photo with no analysis copy
    yet (the backfill's to write, and to enqueue keywording for) record nothing.

    Args:
        image: The photo.
        source: The provider's slug.
        outcome: What asking it came to.
    """
    try:
        if outcome is KeywordOutcome.ANSWERED:
            _source_answered(source)
            forget(image.pk, source)
        elif outcome is KeywordOutcome.UNANSWERED or (outcome is KeywordOutcome.NO_INPUT and image.analysis_thumbnail):
            _unanswered(image.pk, source, counts=False)
        elif outcome is KeywordOutcome.BUSY:
            _refused(image.pk, source)
    except DatabaseError:
        logger.exception("Could not record keyword source '%s' %s for image %s", source, outcome, image.pk)


def forget(image_id: int, source: str) -> None:
    """Delete the retry row for a (photo, source), if there is one."""
    from urbanlens.dashboard.models.images.keyword import ImageKeywordRetry

    ImageKeywordRetry.objects.filter(image_id=image_id, source=source).delete()


def _forget_quietly(image_id: int, source: str) -> None:
    try:
        forget(image_id, source)
    except DatabaseError:
        logger.exception("Keyword retry: could not delete the row for '%s' and image %s; it is asked about again", source, image_id)


def _unanswered(image_id: int, source: str, *, counts: bool) -> None:
    """Ask again later; hold the failure against the photo when ``counts``.

    Gives the row up after :data:`MAX_ATTEMPTS` failures held against the photo, or on any failure :data:`MAX_WAIT`
    after its first (``first_failed_at``; a row only refused so far has none).
    """
    from urbanlens.dashboard.models.images.keyword import ImageKeywordRetry

    now = timezone.now()
    row, created = ImageKeywordRetry.objects.get_or_create(image_id=image_id, source=source, defaults={"retry_at": now + _delay(0), "first_failed_at": now})
    if (created and not counts) or row.retry_at is None:
        # New and not held against the photo, or given up already: only an answer clears a given-up row.
        return
    first_failed_at = row.first_failed_at or now
    attempts = min(row.attempts + (1 if counts else 0), MAX_ATTEMPTS)
    overdue = now - first_failed_at >= MAX_WAIT
    retry_at: datetime | None = None if attempts >= MAX_ATTEMPTS or overdue else now + _delay(attempts)
    ImageKeywordRetry.objects.filter(pk=row.pk).update(attempts=attempts, retry_at=retry_at, first_failed_at=first_failed_at, updated=now)
    if retry_at is None:
        reason = f"{attempts} failures of its own" if attempts >= MAX_ATTEMPTS else f"failing for {(now - first_failed_at).days} days"
        logger.warning("Gave up asking '%s' for image %s's keywords after %s", source, image_id, reason)


def _refused(image_id: int, source: str) -> None:
    """Ask again later; a refusal holds nothing against the photo and leaves an existing row as it is."""
    from urbanlens.dashboard.models.images.keyword import ImageKeywordRetry

    ImageKeywordRetry.objects.get_or_create(image_id=image_id, source=source, defaults={"retry_at": timezone.now() + _delay(0)})


# ---------------------------------------------------------------------------
# One source
# ---------------------------------------------------------------------------


def _source_key(source: str) -> str:
    return make_cache_key("keyword-retry:source", source)


def source_backoff(source: str) -> tuple[int, float]:
    """The sweep's backoff for one source.

    Args:
        source: The provider's slug.

    Returns:
        ``(level, until)``: how many runs in a row the source's first call failed, and the epoch second it may be
        asked again. ``(0, 0.0)`` when it is not backing off, or the cache cannot say.
    """
    try:
        state: Any = cache.get(_source_key(source))
    except _CACHE_ERRORS:
        logger.warning("Keyword retry: could not read the backoff for '%s'; asking it", source, exc_info=True)
        return 0, 0.0
    if not isinstance(state, list | tuple) or len(state) != 2:
        return 0, 0.0
    try:
        return int(state[0]), float(state[1])
    except (TypeError, ValueError):
        return 0, 0.0


def _source_failed(source: str) -> None:
    from urbanlens.dashboard.services.core.provider_health import backoff_duration

    level = source_backoff(source)[0] + 1
    until = time.time() + backoff_duration(level).total_seconds()
    try:
        cache.set(_source_key(source), (level, until), _SOURCE_STATE_TTL_SECONDS)
    except _CACHE_ERRORS:
        logger.warning("Keyword retry: could not record the backoff for '%s'", source, exc_info=True)
    logger.info("Keyword retry: '%s' did not answer; not asked again for %ds (failing run %d in a row)", source, until - time.time(), level)


def _source_answered(source: str) -> None:
    try:
        cache.delete(_source_key(source))
    except _CACHE_ERRORS:
        logger.warning("Keyword retry: could not clear the backoff for '%s'", source, exc_info=True)


def _closed_reason(provider: PhotoKeywordProvider) -> str | None:
    """Why the sweep must not ask ``provider`` at all this run, or None to ask it. Takes no rate-limit or probe slot."""
    from urbanlens.dashboard.services.core import provider_health
    from urbanlens.dashboard.services.core.egress import egress_permitted
    from urbanlens.dashboard.services.core.rate_limiter import service_is_enabled

    service = provider.service_key
    if service:
        if not egress_permitted(service):
            return "not called in this environment"
        if not service_is_enabled(service):
            return "switched off"
        if provider_health.backed_off(service):
            return "backed off by provider health"
    level, until = source_backoff(provider.slug)
    if until > time.time():
        return f"backing off after {level} failing run(s) in a row"
    return None


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _due_rows(sources: list[str], *, limit: int, now: datetime) -> list[ImageKeywordRetry]:
    """Up to ``limit`` x :data:`_WINDOW_PER_PHOTO` due rows per source, the longest-waiting first across all of them."""
    from urbanlens.dashboard.models.images.keyword import ImageKeywordRetry

    rows: list[ImageKeywordRetry] = []
    for source in sources:
        rows.extend(ImageKeywordRetry.objects.filter(source=source, retry_at__lte=now).order_by("retry_at", "pk")[: limit * _WINDOW_PER_PHOTO])
    rows.sort(key=lambda row: (row.retry_at or now, row.pk))
    return rows


def _retry(row: ImageKeywordRetry, provider: PhotoKeywordProvider) -> str:
    """Ask ``provider`` about the row's photo again, and record what came of it, except a failure.

    A failure, and an analysis copy that could not be read, are recorded by :func:`sweep` once the run is over, when it
    knows whether the source answered anyone and whether any copy could be read.
    """
    from urbanlens.dashboard.models.images.model import Image, MediaKind
    from urbanlens.dashboard.services.photos.photo_keywords import run_keyword_provider

    image = Image.objects.select_related("profile__user").filter(pk=row.image_id).first()
    if image is None or not image.image or image.media_type != MediaKind.PHOTO or image.profile is None or not image.profile.generate_photo_keywords:
        _forget_quietly(row.image_id, row.source)
        return DROPPED

    outcome, _stored = run_keyword_provider(image, provider)
    if outcome is KeywordOutcome.ANSWERED:
        # The answer first: a database error deleting the row must not leave the source looking down.
        _source_answered(row.source)
        _forget_quietly(image.pk, row.source)
    elif outcome is KeywordOutcome.NOT_AVAILABLE or (outcome is KeywordOutcome.NO_INPUT and not image.analysis_thumbnail):
        # No longer this source's to answer, or the backfill's: it writes the copy and enqueues keywording itself.
        _forget_quietly(image.pk, row.source)
        return DROPPED
    # BUSY and OFF: refused before anything was sent. The row stays due, and nothing is held against the photo.
    return outcome


def _settle(failed: dict[str, list[int]], unreadable: list[tuple[int, str]], *, answered: set[str], read_a_copy: bool) -> None:
    """Record a run's failures and unreadable copies, and back off each source that failed and answered nothing."""
    for source, image_ids in failed.items():
        counts = source in answered
        for image_id in image_ids:
            try:
                _unanswered(image_id, source, counts=counts)
            except DatabaseError:
                logger.exception("Keyword retry: could not record '%s' failing for image %s", source, image_id)
        if not counts:
            _source_failed(source)
    for image_id, source in unreadable:
        try:
            _unanswered(image_id, source, counts=read_a_copy)
        except DatabaseError:
            logger.exception("Keyword retry: could not record image %s's unreadable analysis copy", image_id)


def sweep(limit: int = SWEEP_BATCH) -> Counter[str]:
    """Ask each keyword source again about the photos it did not answer for, within this run's limits.

    Args:
        limit: The most photos to ask a source about.

    Returns:
        How many rows came to each :class:`KeywordOutcome`, or :data:`DROPPED`.
    """
    from urbanlens.dashboard.plugins.registry import plugin_registry
    from urbanlens.dashboard.services.core.background_work import background_work

    outcomes: Counter[str] = Counter()
    open_sources: dict[str, PhotoKeywordProvider] = {}
    for provider in plugin_registry.photo_keyword_providers():
        if not provider.slug or provider.slug in open_sources:
            continue
        reason = _closed_reason(provider)
        if reason is not None:
            logger.info("Keyword retry: not asking '%s' this run: %s", provider.slug, reason)
            continue
        open_sources[provider.slug] = provider
    if not open_sources:
        return outcomes

    started = time.monotonic()
    asked = 0
    answered: set[str] = set()
    failed: dict[str, list[int]] = {}
    unreadable: list[tuple[int, str]] = []
    in_a_row: Counter[str] = Counter()
    try:
        # Background work whoever runs it, so provider health stops it first.
        with background_work():
            for row in _due_rows(list(open_sources), limit=limit, now=timezone.now()):
                if asked >= limit or time.monotonic() - started >= SWEEP_TIME_BUDGET_SECONDS:
                    break
                source = open_sources.get(row.source)
                if source is None:
                    continue
                try:
                    outcome = _retry(row, source)
                except DatabaseError:
                    logger.exception("Keyword retry: could not ask '%s' about image %s again", row.source, row.image_id)
                    continue
                outcomes[outcome] += 1
                if outcome in _ASKED:
                    asked += 1
                if outcome is KeywordOutcome.ANSWERED:
                    answered.add(row.source)
                    in_a_row[row.source] = 0
                elif outcome is KeywordOutcome.UNANSWERED:
                    failed.setdefault(row.source, []).append(row.image_id)
                    in_a_row[row.source] += 1
                    if in_a_row[row.source] >= FAILURES_THAT_STOP_A_SOURCE:
                        del open_sources[row.source]
                elif outcome is KeywordOutcome.NO_INPUT:
                    unreadable.append((row.image_id, row.source))
                elif outcome in _REFUSALS:
                    del open_sources[row.source]
    finally:
        # Also when the soft time limit cuts the run short, so a failure it saw still pushes its row back.
        _settle(failed, unreadable, answered=answered, read_a_copy=any(outcomes[outcome] for outcome in _READ_A_COPY))
    if outcomes:
        logger.info("Keyword retry sweep: %s", ", ".join(f"{outcome} {count}" for outcome, count in sorted(outcomes.items())))
    return outcomes
