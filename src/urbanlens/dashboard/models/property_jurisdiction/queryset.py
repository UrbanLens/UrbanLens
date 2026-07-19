"""QuerySet and manager for PropertyJurisdiction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction


class PropertyJurisdictionQuerySet(abstract.DashboardQuerySet):
    """Query helpers for the county property-jurisdiction registry."""

    def automatable(self) -> Self:
        """Rows the orchestrator has at least one configured tier to try.

        The SQL approximation of ``PropertyJurisdiction.is_automatable``: a
        Tier 1 adapter with an endpoint, or a scraping tier (``vendor`` set /
        ``scrape_recipe`` present) not vetoed by ``requires_captcha``. Whether
        a ``vendor`` slug actually has a registered template is a code-level
        fact SQL can't see - re-check ``is_automatable`` per row when that
        distinction matters.
        """
        from django.db.models import Q

        from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType

        tier1 = Q(adapter_type__in=[AdapterType.ARCGIS_REST, AdapterType.SOCRATA]) & ~Q(gis_rest_url="")
        scraping = (~Q(vendor="") | ~Q(scrape_recipe={})) & Q(requires_captcha=False)
        return self.exclude(adapter_type=AdapterType.MANUAL_ONLY).filter(tier1 | scraping)

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
            ``(row, created)`` - an existing row's non-blank
            ``county_name``/``state`` are never overwritten by this call, but
            *blank* ones are backfilled when a value is finally known (a row
            first created through a resolution path that couldn't name it -
            discovery needs both to build its search query).
        """
        row, created = self.get_or_create(fips=fips, defaults={"county_name": county_name, "state": state})
        if not created:
            backfill = {name: value for name, value in (("county_name", county_name), ("state", state)) if value and not getattr(row, name)}
            if backfill:
                self.filter(pk=row.pk).update(**backfill)
                for name, value in backfill.items():
                    setattr(row, name, value)
        return row, created
