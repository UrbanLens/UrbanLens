"""LocationCache model - stores external API responses keyed to a shared Location."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db import models
from django.utils import timezone

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.location.model import Location


class LocationCache(abstract.DashboardModel):
    """Caches responses from external data sources keyed to a shared Location.
    An empty-dict ``data`` field means "we searched and found nothing" - this is still a valid cached result so we don't hammer the upstream API again.

    ``audience`` is ``""`` for a row every viewer of the Location may read. A search built from a pin's own names is
    cached under that name set's audience key instead (``services.pins.search_names``), and read only by pins whose
    names produce the same key.

    ``relevance_rule`` is the ``subject_relevance.RULE_VERSION`` the row's results were last swept under
    (``services.media.public_media_sweep``); 0 until then, and again after every write of new results.
    """

    source = models.CharField(max_length=50)
    data = models.JSONField(default=dict)
    query_key = models.CharField(max_length=255, blank=True)
    audience = models.CharField(max_length=64, blank=True, default="", db_default="")
    relevance_rule = models.PositiveSmallIntegerField(default=0, db_default=0)

    location = models.ForeignKey(
        "dashboard.Location",
        on_delete=models.CASCADE,
        related_name="external_cache",
    )

    if TYPE_CHECKING:
        location_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_location_cache"
        unique_together = [("location", "source", "audience")]
        indexes = [
            models.Index(fields=["location", "source"], name="idxdb_loccache_source"),
            models.Index(fields=["source", "relevance_rule"], name="idxdb_loccache_relrule"),
        ]

    @property
    def is_stale(self) -> bool:
        """True if the cached entry is older than the site's configured minimum cache duration."""
        return self.updated < self.fresh_since()

    @classmethod
    def get_fresh(cls, location: Location, source: str, audience: str = "", *, max_age: timedelta | None = None) -> LocationCache | None:
        """Returns a non-stale cache entry, or None if missing or stale.

        Args:
            location: The Location to look up.
            source: Data source identifier (e.g. 'wikipedia').
            audience: Whose row to read; the default is the one every viewer shares.
            max_age: How old the source's answers may be, when that is shorter than the site-wide window.

        Returns:
            A fresh LocationCache instance or None.
        """
        try:
            entry = cls.objects.get(location=location, source=source, audience=audience)
        except cls.DoesNotExist:
            return None
        return None if entry.updated < cls.fresh_since(max_age) else entry

    @classmethod
    def fresh_since(cls, max_age: timedelta | None = None) -> datetime:
        """The oldest ``updated`` a row may have and still be fresh.

        Args:
            max_age: A source's own limit, honoured when it is shorter than the site-wide ``external_data_cache_days``.

        Returns:
            The cutoff.
        """
        from urbanlens.dashboard.models.site_settings import SiteSettings

        window = timedelta(days=SiteSettings.get_current().external_data_cache_days)
        return timezone.now() - (window if max_age is None else min(window, max_age))

    @classmethod
    def fresh_rows(cls, location_id: int, sources: Iterable[str], audiences: Iterable[str], since: datetime | None = None) -> dict[tuple[str, str], LocationCache]:
        """Every non-stale row of these sources and audiences at one Location, in one query.

        Args:
            location_id: The Location's primary key.
            sources: Data source identifiers.
            audiences: The audiences to read.
            since: :meth:`fresh_since`, when the caller already has it.

        Returns:
            ``{(source, audience): row}`` for each fresh row found.
        """
        cutoff = since if since is not None else cls.fresh_since()
        rows = cls.objects.filter(location_id=location_id, source__in=list(sources), audience__in=list(audiences), updated__gte=cutoff)
        return {(row.source, row.audience): row for row in rows}

    @classmethod
    def set(cls, location: Location, source: str, data: dict, query_key: str = "", audience: str = "", *, stale_after: timedelta | None = None) -> LocationCache:
        """Upsert a cache entry.

        Args:
            location: The Location to cache data for.
            source: Data source identifier.
            data: Parsed API response to store.
            query_key: The search term or address used for the lookup.
            audience: Whose row this is; the default is the one every viewer shares.
            stale_after: Let the row go stale this soon instead of after the site-wide window, for an answer its
                upstream said was partial.

        Returns:
            The saved LocationCache instance.
        """
        entry, _ = cls.objects.update_or_create(
            location=location,
            source=source,
            audience=audience,
            defaults={"data": data, "query_key": query_key, "relevance_rule": 0},
        )
        if stale_after is not None:
            # Dated as if written earlier, so every freshness check already in place lets it lapse on time.
            entry.updated = min(entry.updated, cls.fresh_since() + stale_after)
            cls.objects.filter(pk=entry.pk).update(updated=entry.updated)
        return entry
