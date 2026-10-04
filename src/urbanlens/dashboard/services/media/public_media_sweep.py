"""Remove cached public-source results that no one is shown (P233).

P196's relevance rule (``services.media.subject_relevance``) is applied when a gallery is read, so a result about
somewhere else is never shown, but it stays in its ``LocationCache`` row. This removes it from the row once nobody
can be shown it: no reader's subject matches it, and nobody has marked, voted on or copied it.

Readers follow P188's audiences. The shared row is judged by the place's public names alone. A row cached for a set
of custom names is judged by the names of each pin holding that set, and is left alone when no pin holds it any more.

Only the cached results change. The row keeps its age, so it is refetched when it would have been, and no row of
any user's (marks, copies, uploads) is read for anything but its keys. Each row records the ``RULE_VERSION`` it was
judged under, and a write of new results resets that, so a row is judged once per rule and per fetch. A row that
could not be judged (an error, or a fetch rewriting it meanwhile) stays unjudged for the next run.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.services.media import subject_relevance

if TYPE_CHECKING:
    from collections.abc import Collection

    from urbanlens.dashboard.models.cache.location_cache import LocationCache
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.services.media.subject_relevance import MediaSubject
    from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SweepReport:
    """What one sweep did.

    Attributes:
        rows: Cache rows judged.
        removed: Results removed from them.
        remaining: Whether rows were left unjudged when the budget ran out.
    """

    rows: int = 0
    removed: int = 0
    remaining: bool = False


def judged_sources() -> dict[str, GalleryMediaSource]:
    """Every registered gallery source whose results are judged for relevance, by its ``LocationCache`` source."""
    from urbanlens.dashboard.services.pins.external_data import GalleryMediaSource, panel_sources

    return {source.cache_source: source for source in panel_sources().values() if isinstance(source, GalleryMediaSource) and source.judges_relevance}


def sweep_public_media(*, batch_size: int = 100, budget_seconds: float | None = None) -> SweepReport:
    """Judge every cached row not yet judged under the current rule, removing what no one is shown.

    Args:
        batch_size: Rows read per query.
        budget_seconds: Stop after the batch that runs past this, or None to judge every row.

    Returns:
        What the sweep did.
    """
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    report = SweepReport()
    sources = judged_sources()
    if not sources:
        return report
    version = subject_relevance.RULE_VERSION
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    unjudged = LocationCache.objects.filter(source__in=list(sources), relevance_rule__lt=version)
    after = 0
    while batch := list(unjudged.filter(pk__gt=after).select_related("location__place").order_by("pk")[:batch_size]):
        after = batch[-1].pk
        report.rows += len(batch)
        report.removed += _sweep_batch(batch, sources, version)
        if deadline is not None and time.monotonic() >= deadline:
            report.remaining = unjudged.filter(pk__gt=after).exists()
            break
    return report


def _sweep_batch(rows: list[LocationCache], sources: dict[str, GalleryMediaSource], version: int) -> int:
    """Judge one batch of rows and write what changed, returning how many results were removed."""
    by_location: dict[int, list[LocationCache]] = defaultdict(list)
    for row in rows:
        by_location[row.location_id].append(row)
    kept = _touched_keys(by_location.keys())
    unchanged: list[LocationCache] = []
    removed = 0
    for location_id, located in by_location.items():
        try:
            readers = _reader_subjects(located[0].location, {row.audience for row in located})
        except Exception:
            logger.exception("Public-media sweep could not build the subjects for location %s; its rows wait for the next run", location_id)
            continue
        for row in located:
            source = sources[row.source]
            data = row.data if isinstance(row.data, dict) else {}
            try:
                pruned = source.without_irrelevant(data, readers.get(row.audience, ()), kept=kept[location_id])
            except Exception:
                logger.exception("Public-media sweep could not judge cache row %s; it waits for the next run", row.pk)
                continue
            if pruned is None:
                unchanged.append(row)
            elif _write(row, pruned, version):
                removed += len(data[source.media_results_key]) - len(pruned[source.media_results_key])
    _mark_judged(unchanged, version)
    return removed


def _touched_keys(location_ids: Collection[int]) -> dict[int, set[str]]:
    """Per location, the keys of every item anyone has marked, voted on or copied there, under any source."""
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.images.relevance import MediaRelevance

    keys: dict[int, set[str]] = defaultdict(set)
    for location_id, key in MediaRelevance.objects.filter(location_id__in=location_ids).values_list("location_id", "item_key"):
        keys[location_id].add(key)
    copies = Image.objects.filter(location_id__in=location_ids, media_item_key__isnull=False).values_list("location_id", "media_item_key")
    for copy_location_id, copy_key in copies:
        if copy_location_id is not None and copy_key:
            keys[copy_location_id].add(copy_key)
    return keys


def _reader_subjects(location: Location, audiences: set[str]) -> dict[str, tuple[MediaSubject, ...]]:
    """The subjects each of ``audiences``' readers judge its row against; an audience no pin holds is absent."""
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.search_names import SHARED_AUDIENCE, search_names, shared_names

    shared = shared_names(location)
    subjects: dict[str, list[MediaSubject]] = defaultdict(list)
    if SHARED_AUDIENCE in audiences:
        subjects[SHARED_AUDIENCE].append(subject_relevance.subject_for_location(location, shared=shared))
    custom = audiences - {SHARED_AUDIENCE}
    if custom:
        for pin in Pin.objects.filter(location=location):
            pin.location = location
            names = search_names(pin, shared=shared)
            if names.audience in custom and (subject := subject_relevance.subject_for_pin(pin, names=names)) not in subjects[names.audience]:
                subjects[names.audience].append(subject)
    return {audience: tuple(found) for audience, found in subjects.items() if found}


def _write(row: LocationCache, data: dict, version: int) -> bool:
    """Replace ``row``'s results unless a fetch has rewritten it since it was read; its age is left as it was."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    return LocationCache.objects.filter(pk=row.pk, updated=row.updated).update(data=data, relevance_rule=version) == 1


def _mark_judged(rows: list[LocationCache], version: int) -> None:
    """Record ``rows`` as judged under ``version``, skipping any a fetch has rewritten since they were read."""
    from urbanlens.dashboard.models.cache.location_cache import LocationCache

    if not rows:
        return
    unchanged_since_read = Q()
    for row in rows:
        unchanged_since_read |= Q(pk=row.pk, updated=row.updated)
    LocationCache.objects.filter(unchanged_since_read).update(relevance_rule=version)
