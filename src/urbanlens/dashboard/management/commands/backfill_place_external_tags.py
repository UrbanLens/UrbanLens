"""Backfill PlaceExternalTag from already-cached Nominatim/Overture responses.

Shipping PlaceExternalTag only populates it going forward: the Nominatim and
Overture Building Characteristics panel/enrichment sources only sync tags at
fetch time, and only for a Location whose Place was already resolved by then.
Every LocationCache row that predates this feature (source="nominatim" or
"overture_building_attributes") needs one pass to backfill from data already
sitting in the database - no API calls here, so no rate limiting/staggering
is needed the way a live-fetching backfill would.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand

from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, ExtractedTag, PlaceExternalTag
from urbanlens.dashboard.services.locations.external_tags import extract_nominatim_tags, extract_overture_tags

if TYPE_CHECKING:
    from collections.abc import Callable

#: (LocationCache.source, ExternalTagSource, extractor) for every source this
#: feature backfills from.
_SOURCES: tuple[tuple[str, str, Callable[[dict], list[ExtractedTag]]], ...] = (
    ("nominatim", ExternalTagSource.OSM, extract_nominatim_tags),
    ("overture_building_attributes", ExternalTagSource.OVERTURE, extract_overture_tags),
)


class Command(BaseCommand):
    """Backfill PlaceExternalTag rows from LocationCache rows that predate this feature."""

    help = "Backfill PlaceExternalTag from already-cached Nominatim/Overture LocationCache rows. Makes no API calls."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would be written without saving.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        for cache_source, tag_source, extractor in _SOURCES:
            self._backfill_source(cache_source, tag_source, extractor, dry_run=dry_run)

    def _backfill_source(self, cache_source: str, tag_source: str, extractor: Callable[[dict], list[ExtractedTag]], *, dry_run: bool) -> None:
        """Sync one provider's cached data onto every Place it covers.

        A Place can be shared by several Locations (see PlaceExternalTag's
        own docstring on the multi-tenant-Place trade-off) - to stay
        consistent with how the live panel/enrichment sync already behaves
        for that case (whichever Location's fetch runs most recently wins),
        this picks each Place's most-recently-updated LocationCache row of
        this source and ignores any older ones for the same Place, rather
        than merging or accumulating across Locations.

        Args:
            cache_source: The LocationCache.source value to read.
            tag_source: The ExternalTagSource this data becomes.
            extractor: Turns one LocationCache row's data dict into tags.
            dry_run: Report counts without writing.
        """
        self.stdout.write(f"--- {cache_source} -> {tag_source} ---")
        queryset = LocationCache.objects.filter(source=cache_source, location__place__isnull=False).select_related("location__place").order_by("location__place_id", "-updated")

        seen_places: set[int] = set()
        synced = 0
        skipped = 0
        for cache_row in queryset.iterator():
            place = cache_row.location.place
            if place.pk in seen_places:
                continue
            seen_places.add(place.pk)

            tags = extractor(cache_row.data or {})
            if not tags:
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"  [place={place.pk}] would sync {len(tags)} tag(s)")
            else:
                PlaceExternalTag.sync_for_source(place, tag_source, tags)
            synced += 1

        verb = "would sync" if dry_run else "synced"
        self.stdout.write(f"{cache_source}: {verb} {synced} place(s), {skipped} with nothing to extract.")
