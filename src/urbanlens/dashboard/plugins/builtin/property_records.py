"""Property records plugin: automated US county property ownership & tax data.

Implements ``docs/property-records-plan.md``'s tiered retrieval pipeline
(``services.apis.property_records``) as a pin-detail panel and a background
enrichment source. Both share one upstream fetch (``_fetch_payload``, mirroring
the EPA ECHO plugin's ``_fetch_and_cache`` shared-row trick - see
``epa_echo.py``'s module docstring) so whichever runs first for a Location
populates the same ``LocationCache`` row for the other.

A successful fetch also upserts ``WikiOwner``/``WikiPropertySale`` rows with
``source=OwnerSource.OFFICIAL`` - the automated data source those fields were
explicitly reserved for (see ``models.property_owner.meta.OwnerSource``'s own
docstring). This never touches a pre-existing owner/sale record: an OFFICIAL
row is only ever created when no matching owner already exists for that
Location (by name, case-insensitively) - manually-entered data always wins,
matching every other auto-population code path in this codebase (AI link
extraction, name resolution, ...).

Only Tier 1 (ArcGIS REST / Socrata) is implemented - see
``services.apis.property_records.orchestrator``'s module docstring for what
that means for jurisdictions on Tier 2/3/4. This plugin renders nothing for
those (a quiet 204) except the deliberate ``MANUAL_ONLY`` case, which shows a
small card pointing at the county's manual-lookup links instead of silently
disappearing - the plan's explicit ask that "not automatable" surface
clearly rather than fail silently.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.property_records.orchestrator import REASON_MANUAL_ONLY
from urbanlens.dashboard.services.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.external_data import CoordinateGatedInfoPanelSource
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.property_owner.model import WikiOwner
    from urbanlens.dashboard.services.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "property_records"


def _fetch_payload(location: Location) -> dict[str, Any]:
    """Run the orchestrator and return the shared LocationCache payload shape.

    Args:
        location: The Location to fetch a property record for. Its own
            geocoded ``address`` (when already resolved - see
            ``services.enrichment.AddressEnrichmentSource``) is passed
            through as the Tier 2/3 search key; Tier 1's own GIS-derived
            situs address still takes precedence over it when both run (see
            ``orchestrator.get_property_record``'s docstring).

    Returns:
        ``{"available": True, ...PropertyRecord.to_dict()}`` on success, or
        ``{"available": False, "reason": ..., "message": ..., "links": {...}?}``
        - ``links`` (assessor/treasurer/recorder URLs) is only present for
        ``REASON_MANUAL_ONLY``, since that's the only unavailable case this
        plugin renders anything for (see the module docstring).
    """
    from urbanlens.dashboard.services.apis.property_records.orchestrator import PropertyRecordsUnavailableError, get_property_record

    latitude = float(location.latitude or 0)
    longitude = float(location.longitude or 0)
    try:
        record = get_property_record(latitude, longitude, situs_address=location.address or "")
    except PropertyRecordsUnavailableError as exc:
        payload: dict[str, Any] = {"available": False, "reason": exc.reason, "message": str(exc)}
        if exc.reason == REASON_MANUAL_ONLY:
            from urbanlens.dashboard.services.apis.property_records.jurisdiction import resolve_jurisdiction

            jurisdiction = resolve_jurisdiction(latitude, longitude)
            if jurisdiction is not None:
                links = {name: url for name, url in (("assessor_url", jurisdiction.assessor_url), ("treasurer_url", jurisdiction.treasurer_url), ("recorder_url", jurisdiction.recorder_url)) if url}
                if links:
                    payload["links"] = links
        return payload

    payload = record.to_dict()
    payload["available"] = True
    return payload


def _get_or_create_official_owner(location: Location, name: str, *, mailing_address: str = "") -> WikiOwner | None:
    """Find or create an OFFICIAL WikiOwner for this Location, never overwriting an existing one.

    Args:
        location: The Location the owner should be linked to.
        name: The owner's name, as reported by the source.
        mailing_address: Optional mailing address, only used when creating a new row.

    Returns:
        The matched or newly-created WikiOwner, or None for a blank name.
    """
    from urbanlens.dashboard.models.property_owner.meta import OwnerSource
    from urbanlens.dashboard.models.property_owner.model import WikiOwner

    clean_name = (name or "").strip()
    if not clean_name:
        return None

    existing = WikiOwner.objects.for_location(location).filter(name__iexact=clean_name).first()
    if existing is not None:
        return existing

    owner = WikiOwner.objects.create(name=clean_name, source=OwnerSource.OFFICIAL, address=mailing_address or "")
    owner.locations.add(location)
    return owner


def _parse_sale_price(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        price = Decimal(str(raw))
    except InvalidOperation:
        return None
    return price.quantize(Decimal("0.01")) if price.is_finite() and price >= 0 else None


def _write_official_owners_and_sales(location: Location, payload: dict[str, Any]) -> None:
    """Upsert OFFICIAL WikiOwner/WikiPropertySale rows from a successful fetch's payload.

    Deliberately non-destructive and non-authoritative about *current*
    ownership: unlike the manual "record a sale" UI form (which knows a sale
    just happened and unlinks the previous owner - see
    ``controllers.property_owner.WikiPropertySaleTabView``), this only ever
    adds owners/links a Location to them - it never removes an existing
    owner's link, since a single Tier 1 snapshot isn't a trustworthy enough
    signal to override community-visible ownership history.

    Args:
        location: The Location the record belongs to.
        payload: A successful (``available: True``) ``_fetch_payload`` result.
    """
    from urbanlens.dashboard.models.property_owner.meta import OwnerSource
    from urbanlens.dashboard.models.property_owner.model import WikiPropertySale

    mailing_address = payload.get("owner_mailing_address") or ""
    for name in payload.get("owner_name") or []:
        _get_or_create_official_owner(location, name, mailing_address=mailing_address)

    for sale in payload.get("sales_history") or []:
        raw_date = sale.get("date")
        try:
            sale_date = date.fromisoformat(raw_date) if raw_date else None
        except ValueError:
            sale_date = None
        sale_price = _parse_sale_price(sale.get("price"))
        if sale_date is None and sale_price is None:
            continue

        already_recorded = WikiPropertySale.objects.for_location(location).filter(sale_date=sale_date, sale_price=sale_price).exists()
        if already_recorded:
            continue

        new_sale = WikiPropertySale.objects.create(location=location, source=OwnerSource.OFFICIAL, sale_date=sale_date, sale_price=sale_price)
        grantor = _get_or_create_official_owner(location, sale.get("grantor") or "")
        grantee = _get_or_create_official_owner(location, sale.get("grantee") or "")
        if grantor is not None:
            new_sale.previous_owners.add(grantor)
        if grantee is not None:
            new_sale.new_owners.add(grantee)


def _render_available(data: dict[str, Any]) -> dict[str, Any]:
    """Build the info-panel context for a successful record."""
    meta = [{"label": "Situs address", "value": data["situs_address"]}] if data.get("situs_address") else []
    if data.get("apn"):
        meta.append({"label": "APN / Parcel ID", "value": data["apn"]})
    if data.get("land_use_code"):
        meta.append({"label": "Land use", "value": data["land_use_code"]})
    if data.get("lot_size_sqft"):
        meta.append({"label": "Lot size", "value": f"{data['lot_size_sqft']:,.0f} sq ft"})
    if data.get("building_sqft"):
        meta.append({"label": "Building size", "value": f"{data['building_sqft']:,.0f} sq ft"})
    if data.get("year_built"):
        meta.append({"label": "Year built", "value": data["year_built"]})

    assessed = data.get("assessed_value") or {}
    if assessed.get("total"):
        year_suffix = f" ({assessed['year']})" if assessed.get("year") else ""
        meta.append({"label": f"Assessed value{year_suffix}", "value": f"${assessed['total']:,.0f}"})
    if data.get("market_value"):
        meta.append({"label": "Market value", "value": f"${data['market_value']:,.0f}"})

    field_sources = data.get("field_sources") or {}
    distinct_tiers = {data["source"]["tier"], *field_sources.values()}
    chips = [f"Tier {data['source']['tier']}"] if len(distinct_tiers) <= 1 else [f"Tiers {', '.join(str(t) for t in sorted(distinct_tiers))}"]
    chips.append(f"{data['confidence']:.0%} confidence")
    if data.get("field_mismatches"):
        chips.append("Sources disagree")
    if any(entry.get("delinquent") for entry in data.get("tax_history") or []):
        chips.append("Delinquent taxes")

    footer_link = {"url": data["source"]["url"], "label": f"View on {data['source']['provider']}"} if data["source"].get("url") else None

    return {
        "heading_name": ", ".join(data.get("owner_name") or []) or None,
        "chips": chips,
        "meta": meta,
        "footer_link": footer_link,
    }


def _render_manual_only(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build the info-panel context for the one "unavailable" case this panel still shows."""
    links = data.get("links") or {}
    if not links and not data.get("message"):
        return None
    meta = [{"label": label, "value": "Visit site", "href": url} for label, url in (("Assessor", links.get("assessor_url")), ("Treasurer", links.get("treasurer_url")), ("Recorder", links.get("recorder_url"))) if url]
    return {
        "heading_name": data.get("message") or "No automated records for this county",
        "chips": ["Manual lookup required"],
        "meta": meta,
    }


class PropertyRecordsPanelSource(CoordinateGatedInfoPanelSource):
    """County property ownership/tax record card on the pin detail page."""

    key = "property_records"
    cache_source = _CACHE_SOURCE
    section_id = "property-records-section"
    icon = "home_work"
    title = "Property Records"

    def fetch(self, pin: Pin) -> None:
        """Fetch (or reuse the enrichment source's cached fetch of) this pin's property record."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        payload = _fetch_payload(pin.location)
        LocationCache.set(pin.location, self.cache_source, payload, query_key=f"{lat:.5f},{lng:.5f}")
        if payload.get("available"):
            _write_official_owners_and_sales(pin.location, payload)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Render the found record, the manual-only pointer card, or nothing (204)."""
        if not data:
            return None
        if data.get("available"):
            return _render_available(data)
        if data.get("reason") == REASON_MANUAL_ONLY:
            return _render_manual_only(data)
        return None

    def debug_count(self, data: dict) -> int:
        """1 when a record (or a manual-only pointer) was found, else 0."""
        return 1 if (data or {}).get("available") or (data or {}).get("reason") == REASON_MANUAL_ONLY else 0


class PropertyRecordsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the property-records cache (and OFFICIAL owner/sale rows) per Location."""

    key: ClassVar[str] = "property_records"
    verbose_name: ClassVar[str] = "Property Records (county GIS/tax data)"
    cache_source: ClassVar[str] = _CACHE_SOURCE
    service_keys: ClassVar[tuple[str, ...]] = ("census_tigerweb", "property_records_gis")
    usa_only: ClassVar[bool] = True

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Run the orchestrator and, on success, upsert OFFICIAL owner/sale rows.

        Args:
            location: The location to fetch a property record for.

        Returns:
            Tuple of (payload, coordinate query key) - the base class persists
            ``payload`` to the shared ``LocationCache`` row.
        """
        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        payload = _fetch_payload(location)
        if payload.get("available"):
            _write_official_owners_and_sales(location, payload)
        return payload, f"{lat:.5f},{lng:.5f}"


class PropertyRecordsPlugin(UrbanLensPlugin):
    """Automated US county property ownership & tax record retrieval. USA only."""

    name: ClassVar[str] = "property_records"
    verbose_name: ClassVar[str] = "Property Records"
    description: ClassVar[str] = (
        "Free county GIS/open-data lookups (ArcGIS REST / Socrata) for parcel ownership, assessed value, and sale "
        "history, with automatic jurisdiction resolution via the US Census Bureau. Populates the pin/wiki Ownership "
        "and Sale History cards with OFFICIAL-sourced records and shows a details card on the pin detail page. "
        "Coverage depends on the property jurisdiction registry (site-admin) - only ArcGIS/Socrata counties are "
        "automated today; everything else surfaces as 'not automatable' rather than failing silently. USA only."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Census geocoder and the shared county-GIS gateway."""
        return {
            "census_geocoder": ServiceDefaults(
                display_name="US Census Bureau Geocoder",
                calls_per_minute=20,
                calls_per_day=1000,
                usa_only=True,
                notes="Free, keyless API. Used to resolve an address to its county FIPS code.",
            ),
            "property_records_gis": ServiceDefaults(
                display_name="County Property Records (ArcGIS/Socrata)",
                calls_per_minute=30,
                calls_per_day=2000,
                usa_only=True,
                notes="Free county GIS/open-data endpoints, one per jurisdiction (see the property jurisdiction registry). Each host is separately paced regardless of this overall budget.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the pin-detail Property Records card."""
        return [PropertyRecordsPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute background-fill of property records for every pinned Location."""
        return [PropertyRecordsEnrichmentSource()]
