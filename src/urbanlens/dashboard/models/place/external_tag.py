"""PlaceExternalTag - raw classification data external providers attach to a Place.

Strictly separate from the user-facing ``Label`` system (see
``models.labels.model.Label``, whose ``kind=KIND_CATEGORY`` already covers a
different, user-curated notion of "category"). This is provider vocabulary -
OpenStreetMap tags, Overture Maps building attributes - captured so it can
eventually inform label suggestions, icon selection, and search; none of that
is built yet, this only captures and displays the raw data. See
docs/FEATURES.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from django.db import transaction
from django.db.models import CASCADE, BooleanField, CharField, FloatField, ForeignKey, Index, UniqueConstraint

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.place.queryset import PlaceExternalTagManager

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.place.model import Place


class ExternalTagSource(abstract.TextChoices):
    """Which external provider reported a :class:`PlaceExternalTag`."""

    OVERTURE = "overture", "Overture Maps"
    OSM = "osm", "OpenStreetMap"


class ExtractedTag(NamedTuple):
    """One classification tag pulled from a provider's already-fetched response.

    An intermediate value, not a model - built by the extraction helpers in
    ``services.locations.external_tags`` and consumed by
    :meth:`PlaceExternalTag.sync_for_source`.
    """

    key: str
    value: str
    is_primary: bool = False
    confidence: float | None = None


class PlaceExternalTag(abstract.DashboardModel):
    """One classification tag an external provider attaches to a Place.

    Raw provider vocabulary - OpenStreetMap tags, Overture Maps building
    attributes - strictly separate from the user-facing ``Label`` system.

    Attributes:
        place: The Place this tag describes.
        source: Which provider reported it (see :class:`ExternalTagSource`).
        key: The provider's tag/attribute name (e.g. "amenity", "building_subtype").
        value: The raw value (e.g. "restaurant", "single_family_residential").
        is_primary: Whether this is the source's own best/most-specific tag
            for this place, vs. a secondary/supporting one.
        confidence: Provider-reported confidence, when available. Currently
            always null - no source populates it yet - kept for sources that do.
    """

    place = ForeignKey("dashboard.Place", on_delete=CASCADE, related_name="external_tags")
    source = CharField(max_length=20, choices=ExternalTagSource.choices)
    key = CharField(max_length=100)
    value = CharField(max_length=255)
    is_primary = BooleanField(default=False)
    confidence = FloatField(null=True, blank=True)

    objects = PlaceExternalTagManager()

    if TYPE_CHECKING:
        id: int
        place_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_place_external_tags"
        # Primary tags first, so a template can render `place.external_tags.all`
        # directly and have the most useful chip lead without its own sort.
        ordering = ["-is_primary", "source", "key"]
        constraints = [
            UniqueConstraint(fields=["place", "source", "key", "value"], name="place_external_tag_unique"),
        ]
        indexes = [
            Index(fields=["place", "source"], name="idxdb_place_exttag_placesrc"),
        ]

    def __str__(self) -> str:
        return f"{self.get_source_display()}: {self.key}={self.value} (place {self.place_id})"

    @classmethod
    def is_fresh_for(cls, place: Place, source: str) -> bool:
        """Whether ``place`` already has a recent-enough tag set from ``source``.

        Lets ingestion skip re-deriving and rewriting tags when a different
        Location on the same Place already synced within the configured
        window - a Place is coarse and stable (a building's classification
        doesn't change hour to hour), so there is no need to redo this work
        on every nearby fetch. Mirrors ``LocationCache.get_fresh``'s "check
        before you have an instance" shape, and reuses the same staleness
        window that cache uses.

        Args:
            place: The place to check.
            source: Which provider's tags to check the freshness of.

        Returns:
            True if any tag from this source was synced within
            ``SiteSettings.external_data_cache_days``.
        """
        from datetime import timedelta

        from django.utils import timezone

        from urbanlens.dashboard.models.site_settings.model import SiteSettings

        cutoff = timezone.now() - timedelta(days=SiteSettings.get_current().external_data_cache_days)
        return cls.objects.filter(place=place, source=source, updated__gte=cutoff).exists()

    @classmethod
    def sync_for_source(cls, place: Place, source: str, tags: Sequence[ExtractedTag]) -> None:
        """Replace ``place``'s tags from one source with ``tags``, in one transaction.

        A full replace rather than a per-row upsert, so a tag the provider no
        longer reports on a later refetch doesn't linger - mirrors
        ``LocationCache.set``'s "replace the whole payload for this source"
        idiom. Because storage is keyed to Place rather than Location, a
        Place shared by genuinely distinct points of interest (a
        multi-tenant building) will have its tags overwritten by whichever
        Location syncs most recently - accepted for this app's dominant
        one-building-per-Place case, not solved here.

        Args:
            place: The place to sync tags for.
            source: Which provider ``tags`` came from.
            tags: The tags to store; entries with an empty value are skipped.
        """
        with transaction.atomic():
            cls.objects.filter(place=place, source=source).delete()
            cls.objects.bulk_create(cls(place=place, source=source, key=tag.key, value=tag.value, is_primary=tag.is_primary, confidence=tag.confidence) for tag in tags if tag.value)
