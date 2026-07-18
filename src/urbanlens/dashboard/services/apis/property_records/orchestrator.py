"""Ties jurisdiction resolution, tier dispatch, and normalization into one call.

Per ``docs/property-records-plan.md`` section 5 step 5: "tries tiers in order
per property, merges into the standard schema, attaches confidence/source
metadata." Only Tier 1 (``arcgis_socrata.py``) is actually implemented today -
Tiers 2 (known vendor platforms) and 3 (bespoke scraper + LLM-assisted
recipe) are real, tracked follow-up work (see ``docs/PROBLEMS.md``), not
silently-skipped stubs. Calling :func:`get_property_record` for a
jurisdiction assigned to one of those tiers raises
:class:`PropertyRecordsUnavailableError` with a reason distinguishing "not
automatable at all" (Tier 4 / unresearched) from "automatable in principle,
just not built yet" (Tier 2/3) - callers (the panel and enrichment source)
use that distinction to decide whether to show anything at all.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType
from urbanlens.dashboard.services.apis.property_records.arcgis_socrata import ArcGisSocrataGateway
from urbanlens.dashboard.services.apis.property_records.jurisdiction import resolve_jurisdiction
from urbanlens.dashboard.services.apis.property_records.normalize import build_property_record

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
    from urbanlens.dashboard.services.apis.property_records.schema import PropertyRecord

logger = logging.getLogger(__name__)

#: Machine-readable reasons a lookup produced no PropertyRecord.
REASON_OUTSIDE_COVERAGE = "outside_coverage"
REASON_UNRESEARCHED = "unresearched"
REASON_TIER2_NOT_IMPLEMENTED = "tier2_not_implemented"
REASON_TIER3_NOT_IMPLEMENTED = "tier3_not_implemented"
REASON_MANUAL_ONLY = "manual_only"
REASON_NO_DATA_FOUND = "no_data_found"

#: Reasons that mean "will never be automatable without new adapter code" -
#: as opposed to REASON_NO_DATA_FOUND, which is "the adapter ran but this
#: particular parcel wasn't in the results" and is worth retrying later.
PERMANENT_REASONS = frozenset({REASON_OUTSIDE_COVERAGE, REASON_UNRESEARCHED, REASON_TIER2_NOT_IMPLEMENTED, REASON_TIER3_NOT_IMPLEMENTED, REASON_MANUAL_ONLY})


class PropertyRecordsUnavailableError(Exception):
    """Raised instead of returning a record when no tier can (yet) answer for this coordinate.

    Attributes:
        reason: One of the ``REASON_*`` constants above.
        jurisdiction_label: Human-readable jurisdiction name, when known, for
            the caller's own messaging.
    """

    def __init__(self, reason: str, message: str, *, jurisdiction_label: str = "") -> None:
        self.reason = reason
        self.jurisdiction_label = jurisdiction_label
        super().__init__(message)


def get_property_record(latitude: float, longitude: float) -> PropertyRecord:
    """Resolve a coordinate's jurisdiction and retrieve its property record.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.

    Returns:
        A normalized ``PropertyRecord``.

    Raises:
        PropertyRecordsUnavailableError: When the coordinate is outside coverage,
            the jurisdiction has no implemented tier, or the implemented
            tier ran but found nothing for this specific point.
    """
    jurisdiction = resolve_jurisdiction(latitude, longitude)
    if jurisdiction is None:
        raise PropertyRecordsUnavailableError(REASON_OUTSIDE_COVERAGE, "This coordinate isn't in a US county TIGERweb could resolve.")

    label = str(jurisdiction)

    if jurisdiction.adapter_type in (AdapterType.ARCGIS_REST, AdapterType.SOCRATA):
        return _get_tier1_record(jurisdiction, latitude, longitude)

    if jurisdiction.adapter_type == AdapterType.KNOWN_VENDOR:
        raise PropertyRecordsUnavailableError(REASON_TIER2_NOT_IMPLEMENTED, f"{label} uses a known-vendor assessor platform, but Tier 2 adapters aren't implemented yet.", jurisdiction_label=label)

    if jurisdiction.adapter_type == AdapterType.CUSTOM_SCRAPER:
        raise PropertyRecordsUnavailableError(REASON_TIER3_NOT_IMPLEMENTED, f"{label} requires a bespoke scraper, but Tier 3 isn't implemented yet.", jurisdiction_label=label)

    if jurisdiction.adapter_type == AdapterType.MANUAL_ONLY:
        message = jurisdiction.manual_instructions or f"{label} has no digital property records - this requires a phone call, mail, or in-person request."
        raise PropertyRecordsUnavailableError(REASON_MANUAL_ONLY, message, jurisdiction_label=label)

    raise PropertyRecordsUnavailableError(REASON_UNRESEARCHED, f"No property-record source has been configured for {label} yet.", jurisdiction_label=label)


def _get_tier1_record(jurisdiction: PropertyJurisdiction, latitude: float, longitude: float) -> PropertyRecord:
    """Run the Tier 1 ArcGIS/Socrata adapter and normalize its best match."""
    raw_results = ArcGisSocrataGateway().query_by_point(jurisdiction, latitude, longitude)
    if not raw_results:
        raise PropertyRecordsUnavailableError(REASON_NO_DATA_FOUND, f"{jurisdiction}'s parcel layer returned nothing for this coordinate.", jurisdiction_label=str(jurisdiction))

    provider = "ArcGIS REST" if jurisdiction.adapter_type == AdapterType.ARCGIS_REST else "Socrata"
    return build_property_record(raw_results[0], jurisdiction=jurisdiction, provider=provider, source_url=jurisdiction.gis_rest_url)
