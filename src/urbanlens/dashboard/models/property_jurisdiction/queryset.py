"""QuerySet and manager for PropertyJurisdiction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction


class PropertyJurisdictionQuerySet(abstract.DashboardQuerySet):
    """Query helpers for the county property-jurisdiction registry."""

    def automatable(self) -> Self:
        """Rows with an implemented Tier 1 adapter (ArcGIS REST or Socrata)."""
        from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType

        return self.filter(adapter_type__in=[AdapterType.ARCGIS_REST, AdapterType.SOCRATA])

    def unresearched(self) -> Self:
        """Rows nobody has configured a retrieval strategy for yet."""
        from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType

        return self.filter(adapter_type=AdapterType.UNKNOWN)


class PropertyJurisdictionManager(abstract.DashboardManager.from_queryset(PropertyJurisdictionQuerySet)):
    """Manager for PropertyJurisdiction."""

    def get_or_create_for_fips(self, fips: str, *, county_name: str = "", state: str = "") -> tuple[PropertyJurisdiction, bool]:
        """Return the registry row for a FIPS code, creating an ``UNKNOWN``-adapter stub if needed.

        Args:
            fips: 5-digit Census FIPS county code.
            county_name: Display name to seed a newly-created row with.
            state: USPS state abbreviation to seed a newly-created row with.

        Returns:
            ``(row, created)`` - an existing row's ``county_name``/``state``
            are never overwritten by this call, only used for a fresh insert.
        """
        return self.get_or_create(fips=fips, defaults={"county_name": county_name, "state": state})
