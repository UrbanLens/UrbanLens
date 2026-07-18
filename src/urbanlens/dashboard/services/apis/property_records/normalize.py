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

from urbanlens.dashboard.services.apis.property_records.field_mapping import map_fields
from urbanlens.dashboard.services.apis.property_records.schema import AssessedValue, PropertyRecord, RecordSource, SaleHistoryEntry

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction

logger = logging.getLogger(__name__)

#: Confidence assigned to a Tier 1 (structured government REST API) record.
#: No competing tier exists yet to weigh against, so this is a flat constant -
#: see schema.py's module docstring on why per-field confidence isn't built yet.
TIER1_CONFIDENCE = 0.7

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


def build_property_record(raw: dict[str, Any], *, jurisdiction: PropertyJurisdiction, provider: str, source_url: str = "") -> PropertyRecord:
    """Normalize one Tier 1 raw attribute dict into a standardized ``PropertyRecord``.

    Args:
        raw: The raw attribute dict from an ArcGIS/Socrata query result.
        jurisdiction: The county registry row this record came from (supplies
            county/state/FIPS and any field-name override).
        provider: Human-readable provider label for ``source.provider``.
        source_url: The exact endpoint queried, for ``source.url``.

    Returns:
        A ``PropertyRecord`` with ``source.tier`` fixed at 1 and
        ``confidence`` at :data:`TIER1_CONFIDENCE` - the only tier this
        builder supports (see the module docstring).
    """
    mapped = map_fields(raw, jurisdiction.field_map)
    apn = _clean_str(mapped.get("apn"))

    return PropertyRecord(
        situs_address=_clean_str(mapped.get("situs_address")),
        county=jurisdiction.county_name,
        state=jurisdiction.state,
        fips=jurisdiction.fips,
        source=RecordSource(tier=1, provider=provider, url=source_url),
        confidence=TIER1_CONFIDENCE,
        parcel_id=apn,
        apn=apn,
        owner_name=_split_owner_names(mapped.get("owner_name")),
        owner_mailing_address=_clean_str(mapped.get("owner_mailing_address")) or None,
        legal_description=_clean_str(mapped.get("legal_description")) or None,
        land_use_code=_clean_str(mapped.get("land_use_code")) or None,
        lot_size_sqft=_to_float(mapped.get("lot_size_sqft")),
        building_sqft=_to_float(mapped.get("building_sqft")),
        year_built=_to_int(mapped.get("year_built")),
        assessed_value=_build_assessed_value(mapped),
        market_value=_to_float(mapped.get("market_value")),
        sales_history=_build_sales_history(mapped),
    )
