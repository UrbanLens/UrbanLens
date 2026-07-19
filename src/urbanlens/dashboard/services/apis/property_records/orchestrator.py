"""Ties jurisdiction resolution, tier dispatch, and per-field merging into one call.

Per ``docs/property-records-plan.md`` section 5 step 5: "tries tiers in order
per property, merges into the standard schema, attaches confidence/source
metadata." Every tier that has real configuration on a jurisdiction's
``PropertyJurisdiction`` row is attempted - not just one fixed "the"
tier - and whatever succeeds is merged field-by-field (``merge.merge_records``,
preferring the lower/more-structured tier per field, per plan section 4).
This lets a county with, say, a Tier 1 GIS layer AND a hand-written Tier 3
recipe combine both (e.g. geometry-derived situs address from Tier 1, tax
payment status only the Tier 3 site has) rather than being stuck on
whichever one happens to be marked as the jurisdiction's "primary" tier.

``AdapterType.MANUAL_ONLY`` is the one explicit short-circuit: an operator
who has flagged a jurisdiction that way is stating "nothing here is
automatable," which is trusted outright rather than second-guessed by trying
a possibly-stale leftover ``scrape_recipe``. ``requires_captcha`` similarly
vetoes the scraping tiers (2/3) - the plan's compliance rule that a
CAPTCHA-fronted search is never attempted programmatically - but not Tier 1,
whose REST/open-data endpoints sit behind no such gate.

Tier 2/3 need a search key (situs address or APN) that Tier 1's point query
doesn't - callers may pass one in (``situs_address``/``apn``, e.g. a pin's
``Location.address``); if Tier 1 also runs and yields its own (fresher,
GIS-derived) situs address, that takes precedence for any Tier 2/3 attempts
made in the same call.

Each tier attempt reports one of five outcomes, tracked so the final "why
nothing" error picks the most informative reason once every tier has been
tried and none produced a record:

- ``"not_configured"`` - nothing on the jurisdiction row points at this tier
  at all (no vendor set / no template for it, no scrape_recipe, no Tier 1
  endpoint).
- ``"blocked"`` - configured, but deliberately never attempted
  (``requires_captcha`` vetoes Tier 2/3).
- ``"no_data"`` - configured and fetched/queried, but nothing usable came back.
- ``"error"`` - configured, attempted, and the source couldn't be reached
  (transient - see ``meta.SourceUnreachableError``).
- ``"ok"`` - produced a record.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.property_jurisdiction.meta import AdapterType
from urbanlens.dashboard.services.apis.property_records import vendor_templates
from urbanlens.dashboard.services.apis.property_records.arcgis_socrata import ArcGisSocrataGateway
from urbanlens.dashboard.services.apis.property_records.html_scrape import execute_scrape_recipe, recipe_from_dict
from urbanlens.dashboard.services.apis.property_records.jurisdiction import resolve_jurisdiction
from urbanlens.dashboard.services.apis.property_records.merge import merge_records
from urbanlens.dashboard.services.apis.property_records.meta import SourceUnreachableError
from urbanlens.dashboard.services.apis.property_records.normalize import TIER1_CONFIDENCE, TIER2_CONFIDENCE, TIER3_CONFIDENCE, build_property_record

if TYPE_CHECKING:
    from urbanlens.dashboard.models.property_jurisdiction.model import PropertyJurisdiction
    from urbanlens.dashboard.services.apis.property_records.html_scrape import ScrapeRecipe
    from urbanlens.dashboard.services.apis.property_records.schema import PropertyRecord

logger = logging.getLogger(__name__)

#: Machine-readable reasons a lookup produced no PropertyRecord.
REASON_OUTSIDE_COVERAGE = "outside_coverage"
REASON_UNRESEARCHED = "unresearched"
REASON_TIER2_NOT_IMPLEMENTED = "tier2_not_implemented"
REASON_TIER3_NOT_IMPLEMENTED = "tier3_not_implemented"
REASON_MANUAL_ONLY = "manual_only"
REASON_BLOCKED = "blocked"
REASON_NO_DATA_FOUND = "no_data_found"
REASON_SOURCE_ERROR = "source_error"

#: Reasons that mean "will never be automatable without new adapter code or
#: registry data" - as opposed to REASON_NO_DATA_FOUND ("every configured
#: tier ran but this parcel wasn't in any of their results" - worth retrying
#: once the cache naturally expires) and REASON_SOURCE_ERROR ("a configured
#: source is down right now" - transient, must not be negative-cached at all;
#: see ``plugins.builtin.property_records._fetch_payload``).
PERMANENT_REASONS = frozenset(
    {REASON_OUTSIDE_COVERAGE, REASON_UNRESEARCHED, REASON_TIER2_NOT_IMPLEMENTED, REASON_TIER3_NOT_IMPLEMENTED, REASON_MANUAL_ONLY, REASON_BLOCKED},
)

_NOT_CONFIGURED = "not_configured"
_BLOCKED = "blocked"
_NO_DATA = "no_data"
_ERROR = "error"
_OK = "ok"


class PropertyRecordsUnavailableError(Exception):
    """Raised instead of returning a record when no tier can (yet) answer for this coordinate.

    Attributes:
        reason: One of the ``REASON_*`` constants above.
        jurisdiction_label: Human-readable jurisdiction name, when known, for
            the caller's own messaging.
        links: The jurisdiction's manual-lookup reference URLs
            (``assessor_url``/``treasurer_url``/``recorder_url``), populated
            for the reasons where pointing a human at the county's own site
            is the only remaining path (``manual_only``/``blocked``) - so
            callers never need a second jurisdiction resolution round-trip
            just to render those links.
    """

    def __init__(self, reason: str, message: str, *, jurisdiction_label: str = "", links: dict[str, str] | None = None) -> None:
        self.reason = reason
        self.jurisdiction_label = jurisdiction_label
        self.links = links or {}
        super().__init__(message)


def _manual_links(jurisdiction: PropertyJurisdiction) -> dict[str, str]:
    """The jurisdiction's non-empty human-facing reference URLs, keyed by field name."""
    candidates = (("assessor_url", jurisdiction.assessor_url), ("treasurer_url", jurisdiction.treasurer_url), ("recorder_url", jurisdiction.recorder_url))
    return {name: url for name, url in candidates if url}


def get_property_record(latitude: float, longitude: float, *, situs_address: str = "", apn: str = "") -> PropertyRecord:
    """Resolve a coordinate's jurisdiction and retrieve its merged property record.

    Args:
        latitude: WGS-84 latitude.
        longitude: WGS-84 longitude.
        situs_address: The property's already-known street address, if any -
            used as the Tier 2/3 search key when Tier 1 doesn't independently
            supply one (e.g. a pin's ``Location.address``).
        apn: The property's already-known parcel/APN, if any - same role as
            ``situs_address`` for a jurisdiction whose Tier 2/3 recipe
            searches by parcel number instead.

    Returns:
        A ``PropertyRecord`` merged from every tier that had data (see the
        module docstring) - identical to a single tier's own record when
        only one tier answered.

    Raises:
        PropertyRecordsUnavailableError: When the coordinate is outside
            coverage, the jurisdiction is unresearched/manual-only, every
            configured tier is blocked or unimplemented, every tier that did
            run found nothing for this specific point, or (transiently -
            ``REASON_SOURCE_ERROR``) a configured source couldn't be reached.
    """
    jurisdiction = resolve_jurisdiction(latitude, longitude)
    if jurisdiction is None:
        raise PropertyRecordsUnavailableError(REASON_OUTSIDE_COVERAGE, "This coordinate isn't in a US county TIGERweb could resolve.")

    label = str(jurisdiction)

    if jurisdiction.adapter_type == AdapterType.MANUAL_ONLY:
        message = jurisdiction.manual_instructions or f"{label} has no digital property records - this requires a phone call, mail, or in-person request."
        raise PropertyRecordsUnavailableError(REASON_MANUAL_ONLY, message, jurisdiction_label=label, links=_manual_links(jurisdiction))

    tier1_status, tier1_record = _try_tier1(jurisdiction, latitude, longitude)

    effective_address = (tier1_record.situs_address if tier1_record and tier1_record.situs_address else situs_address) or ""
    effective_apn = (tier1_record.apn if tier1_record and tier1_record.apn else apn) or ""

    tier2_status, tier2_record = _try_tier2(jurisdiction, effective_address, effective_apn)
    tier3_status, tier3_record = _try_tier3(jurisdiction, effective_address, effective_apn)

    attempts = [record for record in (tier1_record, tier2_record, tier3_record) if record is not None]
    if attempts:
        return merge_records(attempts)

    raise _build_unavailable_error(jurisdiction, label, [tier1_status, tier2_status, tier3_status])


def _build_unavailable_error(jurisdiction: PropertyJurisdiction, label: str, statuses: list[str]) -> PropertyRecordsUnavailableError:
    """Pick the most informative reason once every tier attempt has come up empty."""
    attempted = [status for status in statuses if status in (_NO_DATA, _ERROR)]

    if not attempted:
        if _BLOCKED in statuses:
            message = f"{label}'s property search site uses a CAPTCHA, so automated lookups are disabled for it."
            return PropertyRecordsUnavailableError(REASON_BLOCKED, message, jurisdiction_label=label, links=_manual_links(jurisdiction))
        if jurisdiction.vendor:
            return PropertyRecordsUnavailableError(REASON_TIER2_NOT_IMPLEMENTED, f"{label} uses vendor {jurisdiction.vendor!r}, which has no Tier 2 template yet.", jurisdiction_label=label)
        if jurisdiction.adapter_type == AdapterType.CUSTOM_SCRAPER:
            return PropertyRecordsUnavailableError(REASON_TIER3_NOT_IMPLEMENTED, f"{label} is flagged for a bespoke scraper, but no recipe has been written for it yet.", jurisdiction_label=label)
        return PropertyRecordsUnavailableError(REASON_UNRESEARCHED, f"No property-record source has been configured for {label} yet.", jurisdiction_label=label)

    # A source outage outranks "no data": until every configured source has
    # genuinely answered, "this parcel has no data" isn't a fact worth caching.
    if _ERROR in attempted:
        return PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, f"A property-record source for {label} couldn't be reached - try again later.", jurisdiction_label=label)

    return PropertyRecordsUnavailableError(REASON_NO_DATA_FOUND, f"Every configured source for {label} returned nothing for this property.", jurisdiction_label=label)


def _try_tier1(jurisdiction: PropertyJurisdiction, latitude: float, longitude: float) -> tuple[str, PropertyRecord | None]:
    """Run the Tier 1 ArcGIS/Socrata adapter, if configured."""
    if jurisdiction.adapter_type not in (AdapterType.ARCGIS_REST, AdapterType.SOCRATA) or not jurisdiction.gis_rest_url:
        return _NOT_CONFIGURED, None
    if jurisdiction.adapter_type == AdapterType.SOCRATA and not jurisdiction.gis_geo_field:
        logger.warning("Jurisdiction %s is marked Socrata but has no gis_geo_field configured; treating Tier 1 as not configured", jurisdiction.fips)
        return _NOT_CONFIGURED, None

    try:
        raw_results = ArcGisSocrataGateway().query_by_point(jurisdiction, latitude, longitude)
    except SourceUnreachableError:
        logger.info("Tier 1 source unreachable for jurisdiction %s", jurisdiction.fips)
        return _ERROR, None
    if not raw_results:
        return _NO_DATA, None

    provider = "ArcGIS REST" if jurisdiction.adapter_type == AdapterType.ARCGIS_REST else "Socrata"
    record = build_property_record(raw_results[0], jurisdiction=jurisdiction, tier=1, confidence=TIER1_CONFIDENCE, provider=provider, source_url=jurisdiction.gis_rest_url)
    return _OK, record


def _try_tier2(jurisdiction: PropertyJurisdiction, situs_address: str, apn: str) -> tuple[str, PropertyRecord | None]:
    """Run the Tier 2 vendor adapter, if one is registered for this jurisdiction's vendor."""
    template = vendor_templates.get_template(jurisdiction.vendor)
    if template is None:
        return _NOT_CONFIGURED, None
    if jurisdiction.requires_captcha:
        return _BLOCKED, None

    recipe = template.build_recipe(jurisdiction)
    return _run_recipe(recipe, situs_address, apn, tier=2, confidence=TIER2_CONFIDENCE, provider=template.display_name, jurisdiction=jurisdiction, field_map=template.field_map)


def _try_tier3(jurisdiction: PropertyJurisdiction, situs_address: str, apn: str) -> tuple[str, PropertyRecord | None]:
    """Run the Tier 3 bespoke recipe, if one is configured on this jurisdiction row."""
    recipe = recipe_from_dict(jurisdiction.scrape_recipe)
    if recipe is None:
        return _NOT_CONFIGURED, None
    if jurisdiction.requires_captcha:
        return _BLOCKED, None

    return _run_recipe(recipe, situs_address, apn, tier=3, confidence=TIER3_CONFIDENCE, provider=f"{jurisdiction.county_name} custom scraper", jurisdiction=jurisdiction, field_map=None)


def _run_recipe(
    recipe: ScrapeRecipe,
    situs_address: str,
    apn: str,
    *,
    tier: int,
    confidence: float,
    provider: str,
    jurisdiction: PropertyJurisdiction,
    field_map: dict[str, str] | None,
) -> tuple[str, PropertyRecord | None]:
    """Shared Tier 2/3 execution: fetch+extract, normalize."""

    try:
        raw = execute_scrape_recipe(recipe, situs_address=situs_address, apn=apn)
    except SourceUnreachableError:
        logger.info("Tier %s source unreachable for jurisdiction %s", tier, jurisdiction.fips)
        return _ERROR, None
    if not raw:
        return _NO_DATA, None

    record = build_property_record(raw, jurisdiction=jurisdiction, tier=tier, confidence=confidence, provider=provider, source_url=recipe.base_url, field_map=field_map)
    return _OK, record
