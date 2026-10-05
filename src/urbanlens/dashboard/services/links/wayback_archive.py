"""Archiving links at the Wayback Machine, and asking again about the ones it has not archived yet.

A URL is looked up once, however many links name it: an auto link is a ``PinLink`` and a ``WikiLink`` at once, and a
popular page is linked from many pins. What the lookup came to - a snapshot, a failure and when to ask again, or a
refusal for good - is recorded on every link still waiting on the URL.

The Archive's lookups and saves fail often and briefly: a lookup can answer that a page it holds has no snapshot, and
a save it reports failed can still make its capture, so a failed URL is asked about again on a growing wait.
"""

from __future__ import annotations

from collections import Counter
import contextlib
from datetime import timedelta
from enum import StrEnum
import hashlib
import logging
from typing import Any

from django.db.models import Max, Min, Q, QuerySet
from django.db.models.functions import Coalesce
from django.utils import timezone
from redis.exceptions import RedisError
import requests

from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH, PinLink, WikiLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.wayback_machine import WaybackMachineGateway, is_own_site_url
from urbanlens.dashboard.services.core.gateway import GatewayRequestError
from urbanlens.dashboard.services.core.locks import acquire_lock, release_lock
from urbanlens.dashboard.services.security.capability_urls import is_capability_url
from urbanlens.dashboard.services.security.link_urls import is_link_url

logger = logging.getLogger(__name__)

#: The wait before asking again after each failure; after the last, the URL is given up.
RETRY_DELAYS: tuple[timedelta, ...] = (
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(days=1),
    timedelta(days=3),
    timedelta(days=7),
    timedelta(days=30),
)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
#: How long a new link is left to the task its creation queued before a sweep takes it up.
NEW_LINK_GRACE = timedelta(minutes=15)
#: URLs one sweep asks about. Each costs a lookup and perhaps a save, against the Archive's ten calls a minute here.
SWEEP_BATCH = 5
#: The Archive's answers that refuse every caller for now, rather than failing the one page.
_BUSY_STATUSES = frozenset({429, 503})
#: How long one task may hold a URL's lookup before another may try; a save can take a minute and a half.
_LOCK_SECONDS = 180
#: What the cache raises when it cannot answer; ``RuntimeError`` is the test suite's network guard.
_LOCK_CACHE_ERRORS = (RedisError, ConnectionError, OSError, RuntimeError)

_MODELS: dict[str, type[PinLink | WikiLink]] = {"PinLink": PinLink, "WikiLink": WikiLink}


class ArchiveOutcome(StrEnum):
    """What asking about one link came to."""

    ARCHIVED = "archived"
    #: Nothing was asked: the link is gone, has a snapshot, is not due, or another task is asking about its URL.
    SKIPPED = "skipped"
    #: Never to be asked about: the site's own page, a share link, or a snapshot too long to store.
    REFUSED = "refused"
    #: The lookup or the save failed for this URL, which is asked about again later.
    FAILED = "failed"
    #: The Archive, or this deployment's budget for it, refused for now; nothing is held against the URL.
    BUSY = "busy"


def archive_link(link_model: str, link_id: int) -> ArchiveOutcome:
    """Give a link a Wayback Machine snapshot, finding or making one.

    Args:
        link_model: ``"PinLink"`` or ``"WikiLink"``.
        link_id: PK of the link row.

    Returns:
        What came of it.
    """
    model = _MODELS.get(link_model)
    if model is None:
        logger.warning("archive_link: unknown link_model %r", link_model)
        return ArchiveOutcome.SKIPPED

    link = model.objects.filter(pk=link_id).first()
    if link is None or link.wayback_url:
        return ArchiveOutcome.SKIPPED

    if is_own_site_url(link.url) or is_capability_url(link.url):
        _give_up(link.url)
        return ArchiveOutcome.REFUSED

    known = _stored_snapshot(link.url)
    if known:
        return _given(link_model, link_id, link.url, known)

    if link.wayback_attempts >= MAX_ATTEMPTS or (link.wayback_retry_at is not None and link.wayback_retry_at > timezone.now()):
        return ArchiveOutcome.SKIPPED

    lock_key = f"wayback-archive:{hashlib.sha256(link.url.encode()).hexdigest()}"
    try:
        token = acquire_lock(lock_key, _LOCK_SECONDS)
    except _LOCK_CACHE_ERRORS:
        # The lock only saves a duplicate lookup; archive without it rather than not at all.
        token = ""
    if token is None:
        return ArchiveOutcome.SKIPPED
    try:
        return _ask(link_model, link_id, link.url)
    finally:
        if token:
            with contextlib.suppress(*_LOCK_CACHE_ERRORS):
                release_lock(lock_key, token)


def archive_links(link_model: str, link_ids: list[int]) -> dict[int, bool]:
    """Archive a batch of links in turn, leaving the rest to a sweep once the Archive refuses for now.

    Args:
        link_model: ``"PinLink"`` or ``"WikiLink"``.
        link_ids: PKs of the links.

    Returns:
        Whether each link was given a snapshot.

    Raises:
        OSError: A transient failure, so the caller's task retries the batch.
    """
    results = dict.fromkeys(link_ids, False)
    for link_id in link_ids:
        try:
            outcome = archive_link(link_model, link_id)
        except OSError:
            logger.warning("archive_links: transient failure on %s %s, retrying the batch", link_model, link_id)
            raise
        except Exception:
            logger.exception("archive_links: %s %s failed", link_model, link_id)
            continue
        results[link_id] = outcome is ArchiveOutcome.ARCHIVED
        if outcome is ArchiveOutcome.BUSY:
            break
    return results


def sweep(limit: int = SWEEP_BATCH) -> Counter[ArchiveOutcome]:
    """Ask about the links that have come due, the longest-waiting URL first, until the Archive refuses for now.

    Args:
        limit: The most URLs to ask about.

    Returns:
        How many links came to each outcome.
    """
    outcomes: Counter[ArchiveOutcome] = Counter()
    for link_model, link_id in _due_links(limit):
        outcome = archive_link(link_model, link_id)
        outcomes[outcome] += 1
        if outcome is ArchiveOutcome.BUSY:
            break
    return outcomes


def _ask(link_model: str, link_id: int, url: str) -> ArchiveOutcome:
    gateway = WaybackMachineGateway()
    try:
        snapshot = _closest_snapshot(gateway.get_availability(url))
        if not snapshot:
            snapshot = str(gateway.save_url(url).get("archived_url") or "")
    except GatewayRequestError as exc:
        logger.info("archive_link: the Wayback Machine is refused for now: %s", exc)
        return ArchiveOutcome.BUSY
    except requests.RequestException as exc:
        if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in _BUSY_STATUSES:
            logger.info("archive_link: the Wayback Machine answered %s", exc.response.status_code)
            return ArchiveOutcome.BUSY
        logger.warning("archive_link: could not archive %s", url, exc_info=True)
        _note_failure(url)
        return ArchiveOutcome.FAILED

    if not snapshot:
        _note_failure(url)
        return ArchiveOutcome.FAILED
    # The snapshot URL embeds the original, so a near-cap link comes back too long to store.
    if not is_link_url(snapshot, max_length=MAX_LINK_URL_LENGTH):
        logger.info("archive_link: snapshot url for %s %s is not storable", link_model, link_id)
        _give_up(url)
        return ArchiveOutcome.REFUSED
    return _given(link_model, link_id, url, snapshot)


def _closest_snapshot(availability: Any) -> str:
    snapshots = availability.get("archived_snapshots") if isinstance(availability, dict) else None
    closest = snapshots.get("closest") if isinstance(snapshots, dict) else None
    url = closest.get("url") if isinstance(closest, dict) else None
    return url if isinstance(url, str) else ""


def _waiting(model: type[PinLink | WikiLink], url: str) -> QuerySet[PinLink] | QuerySet[WikiLink]:
    return model.objects.filter(url=url, wayback_url="")


def _stored_snapshot(url: str) -> str:
    """A snapshot some link naming *url* already holds, or ""."""
    for model in _MODELS.values():
        stored = model.objects.filter(url=url).exclude(wayback_url="").values_list("wayback_url", flat=True).first()
        if stored:
            return stored
    return ""


def _given(link_model: str, link_id: int, url: str, snapshot: str) -> ArchiveOutcome:
    """Store *snapshot* on every link naming *url* that has none, and say whether link ``link_id`` was one.

    *snapshot* has passed ``is_link_url``, so it is what ``save`` would have stored. A bulk update sends no
    ``post_save``, so each pin whose link changed is marked changed here, as ``resync_pin_on_link_saved`` would have
    marked it: the external API's sync feed pages by ``Pin.updated``.
    """
    now = timezone.now()
    given = False
    for name, model in _MODELS.items():
        waiting = _waiting(model, url)
        given = given or (name == link_model and waiting.filter(pk=link_id).exists())
        pin_ids = list(waiting.values_list("pin_id", flat=True)) if model is PinLink else []
        waiting.update(wayback_url=snapshot, wayback_retry_at=None, updated=now)
        if pin_ids:
            Pin.objects.filter(pk__in=pin_ids).update(updated=now)
    return ArchiveOutcome.ARCHIVED if given else ArchiveOutcome.SKIPPED


def _note_failure(url: str) -> None:
    """Count a failed attempt against every link waiting on *url*, and set when it may be asked about again."""
    attempts = 1 + max(_waiting(model, url).aggregate(most=Max("wayback_attempts"))["most"] or 0 for model in _MODELS.values())
    attempts = min(attempts, MAX_ATTEMPTS)
    retry_at = timezone.now() + RETRY_DELAYS[attempts - 1] if attempts < MAX_ATTEMPTS else None
    for model in _MODELS.values():
        _waiting(model, url).update(wayback_attempts=attempts, wayback_retry_at=retry_at)


def _give_up(url: str) -> None:
    for model in _MODELS.values():
        _waiting(model, url).update(wayback_attempts=MAX_ATTEMPTS, wayback_retry_at=None)


def _due_links(limit: int) -> list[tuple[str, int]]:
    """One link per URL that has come due, the longest-waiting first.

    A link never asked about is due once its own task has had :data:`NEW_LINK_GRACE` to run.
    """
    now = timezone.now()
    due = Q(wayback_retry_at__lte=now) | Q(wayback_retry_at__isnull=True, created__lte=now - NEW_LINK_GRACE)
    candidates: list[tuple[Any, str, str, int]] = []
    for name, model in _MODELS.items():
        rows = model.objects.filter(due, wayback_url="", wayback_attempts__lt=MAX_ATTEMPTS).values("url").annotate(link_id=Min("pk"), since=Min(Coalesce("wayback_retry_at", "created"))).order_by("since", "link_id")[:limit]
        candidates.extend((row["since"], row["url"], name, row["link_id"]) for row in rows)
    chosen: dict[str, tuple[str, int]] = {}
    for _since, url, name, link_id in sorted(candidates, key=lambda candidate: candidate[0]):
        chosen.setdefault(url, (name, link_id))
    return list(chosen.values())[:limit]
