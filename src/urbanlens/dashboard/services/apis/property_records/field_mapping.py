"""Maps a Tier 1 (ArcGIS/Socrata) source's raw attribute dict onto the standardized schema.

There is no universal field-naming standard across county GIS layers - see
``docs/property-records-plan.md`` section 5 step 2 ("one client, since the
query pattern is standard" refers to the *query* pattern, not field names).
This module resolves raw attribute names to :class:`~schema.PropertyRecord`
fields in two passes:

1. An explicit per-jurisdiction override (``PropertyJurisdiction.field_map``),
   for a county whose field names don't match the heuristics below.
2. A heuristic best-effort match against common field-name spellings
   observed across county assessor GIS layers, so a brand-new jurisdiction
   with no manual configuration still often produces a usable record.

Matching is done on a normalized form of both the candidate and the raw key
(uppercased, non-alphanumeric characters stripped) so ``"Owner Name"``,
``"OWNER_NAME"``, and ``"OwnerName"`` are all treated as the same field.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

#: Standard field name -> candidate raw attribute names, most-common-first.
#: Matched after normalization (see ``_normalize_key``), so case/punctuation
#: differences in the raw key don't matter.
_HEURISTIC_CANDIDATES: dict[str, tuple[str, ...]] = {
    # UPI ("Uniform Parcel Identifier") confirmed live: a real, previously-verified-correct
    # county (Chester County, PA) uses UPI/OWN1/LOC_ADDRESS/CLASS/TOT_ASSESS/TAXYR - none of
    # which matched any candidate below, so every core field silently came back empty for a
    # jurisdiction the discovery pipeline had specifically confirmed as genuinely comprehensive
    # parcel data. Found while live-verifying this module's own new geometry-capture code
    # against real data, not a hypothetical gap.
    "apn": ("APN", "PARCELID", "PARCEL_ID", "PARCELNO", "PARCEL_NO", "PIN", "PROPID", "PROPERTYID", "TAXID", "TAX_ID", "PARCELNUMBER", "UPI"),
    "owner_name": ("OWNER", "OWNERNME1", "OWNERNAME", "OWNER_NAME", "OWNNAME", "TAXPAYER", "TAXPAYERNAME", "OWNER1", "OWN1"),
    "owner_mailing_address": ("MAILADD", "MAILADDR", "MAIL_ADDR", "MAILINGADDRESS", "OWNERADDR", "OWNERMAILADDRESS", "MAILADDRESS"),
    "situs_address": ("SITUS", "SITEADDR", "SITE_ADDR", "SITUSADDR", "SITUSADDRESS", "PROPADDR", "PROPERTYADDRESS", "ADDRESS", "FULLADDR", "LOCADDR", "LOC_ADDRESS"),
    "legal_description": ("LEGALDESC", "LEGAL_DESC", "LEGALDESCRIPTION", "LEGAL"),
    "land_use_code": ("LANDUSE", "USECODE", "USE_CODE", "PROPCLASS", "PROPERTYCLASS", "LANDUSECODE", "DORCODE"),
    "building_sqft": ("BLDGSQFT", "BLDG_SQFT", "TOTALSQFT", "TOTAL_SQFT", "LIVINGAREA", "LIVAREA", "HEATEDAREA", "SQFT", "BUILDINGAREA"),
    "year_built": ("YEARBUILT", "YR_BLT", "YRBLT", "EFFYRBLT", "ACTUALYEARBUILT", "YEARBLT"),
    "assessed_land": ("LANDVAL", "LAND_VALUE", "ASSDLAND", "ASMT_LAND", "LANDASSESSEDVALUE", "LANDVALUE"),
    "assessed_improvement": ("IMPVAL", "IMP_VALUE", "ASSDIMP", "ASMT_IMP", "IMPROVEMENTVALUE", "BLDGVAL", "IMPROVEMENTSVALUE"),
    "assessed_total": ("TOTALVAL", "TOTAL_VALUE", "ASSDTOTAL", "ASMT_TOTAL", "TOTALASSESSEDVALUE", "TAXVALUE", "ASSESSEDVALUE", "TOT_ASSESS"),
    "assessed_year": ("ASSESSYEAR", "ASMT_YEAR", "TAXYEAR", "ASSESSMENTYEAR", "ASSESSEDYEAR", "TAXYR"),
    "market_value": ("MARKETVAL", "MARKET_VALUE", "JUSTVAL", "JUST_VALUE", "FMV", "FAIRMARKETVALUE", "MARKETVALUE"),
    "sale_price": ("SALEPRICE", "SALE_PRICE", "LASTSALEPRICE", "LSALEPRICE"),
    "sale_date": ("SALEDATE", "SALE_DATE", "LASTSALEDATE", "LSALEDATE"),
}

#: Supplementary fields captured when present, but deliberately kept out of
#: :data:`PARCEL_FIELD_CANDIDATES` (``relevance.py``'s "does this look like
#: comprehensive parcel data" signal) - retrieval-only. A live discovery
#: false positive (Pima County, AZ's "DisadvantagedStatus_SFResidentialParcels"
#: layer - a single-family-only subset of the county, not its real
#: comprehensive parcel data) had STORIES/ROOF/GARAGE/ZONING-shaped fields;
#: folding these into the same pool discovery uses to judge comprehensiveness
#: would make that exact false positive pass again. Building-characteristic
#: fields describe *a* building, not "this is the county's real assessor
#: database" - only :data:`_HEURISTIC_CANDIDATES` above carries that signal.
_SUPPLEMENTARY_CANDIDATES: dict[str, tuple[str, ...]] = {
    "owner_mailing_city": ("MAILCITY", "MAIL_CITY", "OWNERCITY", "OWNER_CITY"),
    "owner_mailing_state": ("MAILSTATE", "MAIL_STATE", "OWNERSTATE", "OWNER_STAT", "OWNER_STATE"),
    "owner_mailing_zip": ("MAILZIP", "MAIL_ZIP", "OWNERZIP", "OWNER_ZIP"),
    "co_owner_name": ("COOWNER", "CO_OWNER", "OWNERNME2", "OWNER2", "CONCTOWNER", "CONCT_OWNER", "OWN2"),
    "zoning_code": ("ZONING", "ZONE", "ZONECLASS", "ZONE_CLASS", "CURZONE", "ZONING_CODE", "CURZONE_OL"),
    "tax_district": ("TAXDIST", "TAX_DIST", "TAXDISTRICT", "TAX_DISTRICT", "LEVYDIST", "LEVY_DIST"),
    "school_district": ("SCHOOLDIST", "SCHOOL_DIST", "SCHOOLDISTRICT", "SCHOOL_DISTRICT", "SCHDIST", "SCH_DIST"),
    "exemption_type": ("EXEMPTTYPE", "EXEMPT_TYPE", "EXEMPTIONTYPE", "EXEMPTION_TYPE", "EXEMPTCODE", "EXEMPT_CODE", "TAXEXEMPTTYPE", "TAXEXEMPT_TYPE"),
    "deferred_value": ("DEFERREDVALUE", "DEFERRED_VALUE", "DEFERVAL", "DEFER_VAL"),
    "subdivision_name": ("SUBDIVNAME", "SUBDIV_NAME", "SUBDIVISION", "SUBDIVISIONNAME"),
    "neighborhood": ("NBHNAME", "NBH_NAME", "NEIGHBORHOOD", "NEIGHBORHOODCODE", "NEIGHBORHOOD_CODE"),
    "prior_parcel_id": ("OLDPIN", "ALTPIN", "PREVPIN", "PRIOR_PIN", "OLDPARCELID", "OLD_PARCEL_ID"),
    "building_stories": ("STORIES", "NUMSTORIES", "NUM_STORIES", "BLDG_STORY", "STORY_HT"),
    "roof_material": ("ROOF", "ROOFTYPE", "ROOF_TYPE", "ROOFMATERIAL", "ROOF_MATERIAL"),
    "wall_material": ("WALLS", "EXTWALL", "EXT_WALL", "WALLTYPE", "WALL_TYPE", "EXTERIORWALL"),
    "garage": ("GARAGE", "GARAGETYPE", "GARAGE_TYPE"),
    "heating_type": ("HEAT", "HEATTYPE", "HEAT_TYPE", "HEATING", "HEATINGTYPE"),
    "building_quality": ("QUALITY", "QUALITYCLASS", "QUALITY_CLASS", "GRADE"),
    "building_condition": ("CONDITION", "CONDITIONCODE", "COND"),
    "building_count": ("NUMBLDGS", "NUM_BLDGS", "BLDGCOUNT", "BLDG_COUNT", "NUMBUILDINGS"),
    "outbuilding_value": ("OBXFVALUE", "OBXF_VALUE", "OUTBLDGVALUE", "OUTBUILDINGVALUE", "XFOBVALUE"),
}

#: Every candidate ``map_fields`` will actually try to resolve - the union of
#: core identity fields and supplementary ones. Kept separate from
#: :data:`_HEURISTIC_CANDIDATES` itself so :data:`PARCEL_FIELD_CANDIDATES`
#: (imported by ``relevance.py``) only ever reflects the core set.
_ALL_CANDIDATES: dict[str, tuple[str, ...]] = {**_HEURISTIC_CANDIDATES, **_SUPPLEMENTARY_CANDIDATES}

#: Lot-size fields recorded directly in square feet. Deliberately excludes
#: ``SHAPE_AREA``/``SHAPEAREA``: Esri's auto-generated geometry area is in the
#: layer's *projection* units (square meters, or square degrees for WGS-84 -
#: almost never square feet), so treating it as sqft silently reports garbage
#: lot sizes. A county whose layer genuinely stores sqft there can opt in via
#: its ``field_map`` override.
_LOT_SQFT_CANDIDATES = ("LOTSIZE", "LOT_SIZE", "LOTSQFT", "LOT_SQFT")
#: Lot-size fields recorded in acres - converted to square feet by the caller.
_LOT_ACRE_CANDIDATES = ("GISACRE", "ACREAGE", "ACRES", "TOTALACRES", "CALCACRES")

SQFT_PER_ACRE = 43_560.0

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


def _normalize_key(raw_key: str) -> str:
    """Uppercase and strip everything but letters/digits, for tolerant field matching."""
    return _NON_ALNUM.sub("", raw_key.upper())


def _resolve_one(standard_key: str, candidates: tuple[str, ...], normalized_raw: dict[str, str], field_map: dict[str, str] | None) -> str | None:
    """Return the raw attribute name backing one standard field, or None."""
    if field_map and standard_key in field_map:
        # Trusted verbatim: the jurisdiction's own field_map names the raw key
        # exactly. If it's stale/wrong, map_fields' own `raw_key in raw` check
        # (using this exact string) simply drops the field rather than
        # matching something unintended.
        return field_map[standard_key]
    for candidate in candidates:
        raw_key = normalized_raw.get(_normalize_key(candidate))
        if raw_key is not None:
            return raw_key
    return None


def map_fields(raw: dict[str, Any], field_map: dict[str, str] | None = None) -> dict[str, Any]:
    """Resolve a raw attribute dict onto standardized field names.

    Args:
        raw: The raw attribute dict from an ArcGIS/Socrata query result.
        field_map: Optional per-jurisdiction override (``PropertyJurisdiction.field_map``).

    Returns:
        Mapping of standardized field name (schema field names, plus the
        synthetic ``assessed_land``/``assessed_improvement``/``assessed_total``/
        ``assessed_year``/``sale_price``/``sale_date``/``lot_size_sqft``, and
        the supplementary keys in :data:`_SUPPLEMENTARY_CANDIDATES`) to the
        raw value. Only fields that resolved to a present raw key are
        included - a missing field is simply absent, never ``None`` (the
        raw value itself may legitimately be ``None``).
    """
    normalized_raw = {_normalize_key(key): key for key in raw}
    mapped: dict[str, Any] = {}

    for standard_key, candidates in _ALL_CANDIDATES.items():
        raw_key = _resolve_one(standard_key, candidates, normalized_raw, field_map)
        if raw_key is not None and raw_key in raw:
            mapped[standard_key] = raw[raw_key]

    if "lot_size_sqft" not in mapped:
        sqft_key = _resolve_one("lot_size_sqft", _LOT_SQFT_CANDIDATES, normalized_raw, field_map)
        if sqft_key is not None and sqft_key in raw and raw[sqft_key] is not None:
            mapped["lot_size_sqft"] = raw[sqft_key]
        else:
            acre_key = _resolve_one("lot_size_sqft", _LOT_ACRE_CANDIDATES, normalized_raw, field_map)
            if acre_key is not None and acre_key in raw and raw[acre_key] is not None:
                with contextlib.suppress(TypeError, ValueError):
                    mapped["lot_size_sqft"] = float(raw[acre_key]) * SQFT_PER_ACRE

    return mapped
