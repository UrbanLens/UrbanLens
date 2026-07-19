"""Builds a standardized ``PropertyRecord`` from a Tier 1 source's raw attribute dict.

Every value coming out of a county GIS layer is treated as untrusted,
inconsistently-typed external data (ArcGIS returns numbers as either JSON
numbers or numeric strings depending on the field's declared type, and county
data entry is famously inconsistent) - every coercion below fails soft
(``None``/skipped) rather than raising, so one malformed field never discards
an otherwise-usable record.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
import logging
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.services.apis.property_records.arcgis_socrata import GEOMETRY_KEY
from urbanlens.dashboard.services.apis.property_records.field_mapping import map_fields
from urbanlens.dashboard.services.apis.property_records.schema import AssessedValue, BuildingCharacteristics, PropertyRecord, RecordSource, SaleHistoryEntry

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction

logger = logging.getLogger(__name__)

#: Per-tier confidence constants, reflecting how structured/trustworthy each
#: tier's data shape is - a free structured government REST API (Tier 1) is
#: more reliable than an HTML page scrape (Tier 2/3), and a shared vendor
#: template (Tier 2, battle-tested across many counties) is a bit more
#: reliable than a bespoke per-county recipe (Tier 3, more likely to have an
#: edge case the recipe author didn't anticipate). Used both as the record's
#: own `confidence` for a single-tier result and as the tie-break ranking in
#: `merge.merge_records` when more than one tier answers for the same field.
TIER1_CONFIDENCE = 0.7
TIER2_CONFIDENCE = 0.55
TIER3_CONFIDENCE = 0.45

_OWNER_SPLIT_MARKERS = (" & ", " AND ")


def _clean_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _to_date(value: Any) -> date | None:
    """Parse a date from either an ArcGIS epoch-millisecond timestamp or an ISO/common string."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # ArcGIS date fields are epoch milliseconds (UTC).
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC).date()
        except (ValueError, OverflowError, OSError):
            return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _split_owner_names(raw: Any) -> tuple[str, ...]:
    """Split a single "OWNER1 & OWNER2"-style field into individual names."""
    text = _clean_str(raw)
    if not text:
        return ()
    for marker in _OWNER_SPLIT_MARKERS:
        if marker in text.upper():
            # Split on the marker case-insensitively while preserving original casing.
            parts: list[str] = []
            remainder = text
            while marker in remainder.upper():
                idx = remainder.upper().index(marker)
                parts.append(remainder[:idx].strip())
                remainder = remainder[idx + len(marker) :]
            parts.append(remainder.strip())
            return tuple(part for part in parts if part)
    return (text,)


def _build_owner_names(mapped: dict[str, Any]) -> tuple[str, ...]:
    """Split the primary owner field, then append a separately-reported co-owner (deduplicated)."""
    names = list(_split_owner_names(mapped.get("owner_name")))
    co_owner = _clean_str(mapped.get("co_owner_name"))
    if co_owner and co_owner.casefold() not in {name.casefold() for name in names}:
        names.append(co_owner)
    return tuple(names)


def _build_owner_mailing_address(mapped: dict[str, Any]) -> str | None:
    """Use the source's own combined mailing address, or compose one from separate city/state/zip fields."""
    combined = _clean_str(mapped.get("owner_mailing_address"))
    if combined:
        return combined
    city = _clean_str(mapped.get("owner_mailing_city"))
    state = _clean_str(mapped.get("owner_mailing_state"))
    zip_code = _clean_str(mapped.get("owner_mailing_zip"))
    city_state_zip = " ".join(part for part in (f"{city}," if city and (state or zip_code) else city, state, zip_code) if part)
    return city_state_zip or None


def _build_building_characteristics(mapped: dict[str, Any]) -> BuildingCharacteristics | None:
    stories = _to_float(mapped.get("building_stories"))
    roof_material = _clean_str(mapped.get("roof_material")) or None
    wall_material = _clean_str(mapped.get("wall_material")) or None
    garage = _clean_str(mapped.get("garage")) or None
    heating_type = _clean_str(mapped.get("heating_type")) or None
    quality = _clean_str(mapped.get("building_quality")) or None
    condition = _clean_str(mapped.get("building_condition")) or None
    building_count = _to_int(mapped.get("building_count"))
    outbuilding_value = _to_float(mapped.get("outbuilding_value"))
    if not any((stories is not None, roof_material, wall_material, garage, heating_type, quality, condition, building_count is not None, outbuilding_value is not None)):
        return None
    return BuildingCharacteristics(
        stories=stories,
        roof_material=roof_material,
        wall_material=wall_material,
        garage=garage,
        heating_type=heating_type,
        quality=quality,
        condition=condition,
        building_count=building_count,
        outbuilding_value=outbuilding_value,
    )


def _build_prior_parcel_ids(mapped: dict[str, Any]) -> tuple[str, ...]:
    prior_id = _clean_str(mapped.get("prior_parcel_id"))
    return (prior_id,) if prior_id else ()


def _build_assessed_value(mapped: dict[str, Any]) -> AssessedValue | None:
    land = _to_float(mapped.get("assessed_land"))
    improvement = _to_float(mapped.get("assessed_improvement"))
    total = _to_float(mapped.get("assessed_total"))
    if land is None and improvement is None and total is None:
        return None
    return AssessedValue(year=_to_int(mapped.get("assessed_year")), land=land, improvement=improvement, total=total)


def _build_sales_history(mapped: dict[str, Any]) -> tuple[SaleHistoryEntry, ...]:
    sale_date = _to_date(mapped.get("sale_date"))
    sale_price = _to_float(mapped.get("sale_price"))
    if sale_date is None and sale_price is None:
        return ()
    return (SaleHistoryEntry(sale_date=sale_date, price=sale_price),)


def build_property_record(
    raw: dict[str, Any],
    *,
    jurisdiction: PropertyJurisdiction,
    tier: int,
    confidence: float,
    provider: str,
    source_url: str = "",
    field_map: dict[str, str] | None = None,
) -> PropertyRecord:
    """Normalize one tier's raw label/attribute dict into a standardized ``PropertyRecord``.

    Shared by every tier: Tier 1's raw ArcGIS/Socrata attribute dict and Tier
    2/3's raw scraped label/value dict (``html_scrape.extract_label_value_pairs``)
    are both just "some external system's names for these fields" - the same
    heuristic ``field_mapping.map_fields`` resolution handles both, since
    human-readable page labels ("Owner Name", "Total Assessed Value") survive
    its normalize-then-match logic exactly as well as GIS attribute codes do.

    Args:
        raw: The raw attribute/label dict from the tier's own source.
        jurisdiction: The county registry row this record came from (supplies
            county/state/FIPS and, for Tier 1, the field-name override).
        tier: Which tier produced this data (1, 2, or 3) - becomes ``source.tier``.
        confidence: This tier's confidence constant (e.g. :data:`TIER1_CONFIDENCE`).
        provider: Human-readable provider label for ``source.provider``.
        source_url: The exact endpoint/page queried, for ``source.url``.
        field_map: Field-name override to use instead of
            ``jurisdiction.field_map`` - Tier 2 vendor templates and Tier 3
            recipes don't necessarily share Tier 1's per-jurisdiction mapping.
            Defaults to ``jurisdiction.field_map`` when not given.

    Returns:
        A normalized ``PropertyRecord`` for this one tier's data.
    """
    # __parcel_geometry__ (see arcgis_socrata.GEOMETRY_KEY) carries a Tier 1
    # ArcGIS feature's own boundary alongside its flat attribute dict - never
    # a real county field, and map_fields has no business seeing it.
    geometry = raw.get(GEOMETRY_KEY)
    raw_attributes = {key: value for key, value in raw.items() if key != GEOMETRY_KEY} if GEOMETRY_KEY in raw else raw

    mapped = map_fields(raw_attributes, field_map if field_map is not None else jurisdiction.field_map)
    apn = _clean_str(mapped.get("apn"))

    return PropertyRecord(
        situs_address=_clean_str(mapped.get("situs_address")),
        county=jurisdiction.county_name,
        state=jurisdiction.state,
        fips=jurisdiction.fips,
        source=RecordSource(tier=tier, provider=provider, url=source_url),
        confidence=confidence,
        parcel_id=apn,
        apn=apn,
        owner_name=_build_owner_names(mapped),
        owner_mailing_address=_build_owner_mailing_address(mapped),
        legal_description=_clean_str(mapped.get("legal_description")) or None,
        land_use_code=_clean_str(mapped.get("land_use_code")) or None,
        lot_size_sqft=_to_float(mapped.get("lot_size_sqft")),
        building_sqft=_to_float(mapped.get("building_sqft")),
        year_built=_to_int(mapped.get("year_built")),
        assessed_value=_build_assessed_value(mapped),
        market_value=_to_float(mapped.get("market_value")),
        sales_history=_build_sales_history(mapped),
        zoning_code=_clean_str(mapped.get("zoning_code")) or None,
        tax_district=_clean_str(mapped.get("tax_district")) or None,
        school_district=_clean_str(mapped.get("school_district")) or None,
        exemption_type=_clean_str(mapped.get("exemption_type")) or None,
        deferred_value=_to_float(mapped.get("deferred_value")),
        subdivision_name=_clean_str(mapped.get("subdivision_name")) or None,
        neighborhood=_clean_str(mapped.get("neighborhood")) or None,
        building_characteristics=_build_building_characteristics(mapped),
        prior_parcel_ids=_build_prior_parcel_ids(mapped),
        parcel_geometry=geometry if isinstance(geometry, dict) else None,
    )
