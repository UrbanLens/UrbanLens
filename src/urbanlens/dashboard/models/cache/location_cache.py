"""LocationCache model - stores external API responses keyed to a shared Location."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db import models
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location


class LocationCache(abstract.Model):
    """
    Caches responses from external data sources keyed to a shared Location.

    An empty-dict ``data`` field means "we searched and found nothing" - this
    is still a valid cached result so we don't hammer the upstream API again.
    A missing row means the source has never been queried for this location.
    """

    STALE_AFTER_DAYS = 7

    location = models.ForeignKey(
        "dashboard.Location",
        on_delete=models.CASCADE,
        related_name="external_cache",
    )
    source = models.CharField(max_length=50)

    if TYPE_CHECKING:
        location_id: int
    data = models.JSONField(default=dict)
    query_key = models.CharField(max_length=255, blank=True)

    class Meta(abstract.Model.Meta):
        db_table = "dashboard_location_cache"
        unique_together = [("location", "source")]
        indexes = [
            models.Index(fields=["location", "source"], name="idxdb_loccache_source"),
        ]

    @property
    def is_stale(self) -> bool:
        """True if the cached entry is older than STALE_AFTER_DAYS."""
        return timezone.now() - self.updated > timedelta(days=self.STALE_AFTER_DAYS)

    @classmethod
    def get_fresh(cls, location: Location, source: str) -> LocationCache | None:
        """
        Returns a non-stale cache entry, or None if missing or stale.

        Args:
            location: The Location to look up.
            source: Data source identifier (e.g. 'wikipedia').

        Returns:
            A fresh LocationCache instance or None.
        """
        try:
            entry = cls.objects.get(location=location, source=source)
        except cls.DoesNotExist:
            return None
        return None if entry.is_stale else entry

    @classmethod
    def set(cls, location: Location, source: str, data: dict, query_key: str = "") -> LocationCache:
        """
        Upsert a cache entry.

        Args:
            location: The Location to cache data for.
            source: Data source identifier.
            data: Parsed API response to store.
            query_key: The search term or address used for the lookup.

        Returns:
            The saved LocationCache instance.
        """
        entry, _ = cls.objects.update_or_create(
            location=location,
            source=source,
            defaults={"data": data, "query_key": query_key},
        )
        return entry
