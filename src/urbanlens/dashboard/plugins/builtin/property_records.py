"""Property records plugin: US county property ownership & tax data via REData.

Renders a pin-detail panel and a background enrichment source from records
fetched over REData's REST API (``services.apis.property_records.redata_gateway``)
- the standalone service that retrieves county property records; how it does
so is REData's own implementation detail. Both panel and enrichment share one
upstream fetch (``_fetch_payload``, mirroring the EPA ECHO plugin's
``_fetch_and_cache`` shared-row trick - see ``epa_echo.py``'s module
docstring) so whichever runs first for a Location populates the same
``LocationCache`` row for the other.

A successful fetch also upserts ``WikiOwner``/``WikiPropertySale`` rows with
``source=OwnerSource.OFFICIAL`` - the automated data source those fields were
explicitly reserved for (see ``models.property_owner.meta.OwnerSource``'s own
docstring). This never touches a pre-existing owner/sale record: an OFFICIAL
row is only ever created when no matching owner already exists for that
Location (by name, case-insensitively) - manually-entered data always wins,
matching every other auto-population code path in this codebase (AI link
extraction, name resolution, ...). This is UrbanLens's own community-data
layer on top of REData's raw facts - REData has no notion of Locations, wikis,
or per-user privacy, and isn't meant to.

Unavailable jurisdictions render nothing (a quiet 204) except the deliberate
"a human must do this" cases - ``MANUAL_ONLY`` and CAPTCHA-``blocked`` - which
show a small card pointing at the county's manual-lookup links instead of
silently disappearing, so "not automatable" surfaces clearly rather than
failing silently. A transient ``source_error`` (REData
unreachable, or a county source it depends on is down) is never cached at all
- the fetch raises so the panel framework's failure-skip/retry machinery
handles it, instead of a days-long ``LocationCache`` row remembering an
outage as "no data".

**Neighbourhood demographics are gated behind ``SiteFeature.NEARBY_RESEARCH``**
(decided 2026-09-08): a census tract's population/income/home-value/rent
figures describe the area around the parcel, not the parcel itself, so they
follow the same "don't show information about anywhere but the pin's own
place to a non-subscriber" rule ``epa_echo.py``'s nearby-facility panel
already applies. **The rest of this card, including the ``containing_park``
chip, stays free for everyone** - a parcel's own facts (situs address, APN,
zoning, tax history, sale records, ``containing_park``) describe the parcel
itself, a real point-in-boundary answer about the pin's own place rather than
somewhere nearby, same as ``owner_name``'s existing ``SiteFeature.PROPERTY_OWNERS``
gate this mirrors.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.property_records.redata_gateway import REASON_BLOCKED, REASON_MANUAL_ONLY, TRANSIENT_REASONS
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, PanelApiKind

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.property_owner.model import WikiOwner
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "property_records"


#: Liens shown on the card. A parcel with a long enforcement history is
#: interesting, but the card is a summary - the full list belongs to whoever
#: goes looking in the county records.
_MAX_LIEN_ROWS = 8


def _coverage_worth_calling(coverage: dict[str, Any], domain: str) -> bool:
    """Whether a coverage-precheck domain is worth an actual supplementary call.

    A missing key, a non-dict entry, or ``available`` not exactly ``False``
    are all treated as "call it" - :meth:`RedataGateway.lookup_coverage` is
    only ever used to *skip* a call REData positively knows is pointless,
    never to withhold one it simply didn't mention (most domains, including
    ``liens``/``tax-payments``, have no coverage key at all).

    Args:
        coverage: :meth:`RedataGateway.lookup_coverage`'s payload - ``{}``
            both for a parcel with no coverage data and for a failed precheck
            (see :func:`_fetch_payload`), which is why this defaults to
            calling rather than skipping.
        domain: The coverage key to check (e.g. ``"assessments"``, ``"sale_records"``).

    Returns:
        False only when ``coverage[domain]["available"]`` is exactly ``False``.
    """
    entry = coverage.get(domain)
    return not (isinstance(entry, dict) and entry.get("available") is False)


def _fetch_payload(location: Location, latitude: float, longitude: float) -> dict[str, Any]:
    """Call REData and return the shared LocationCache payload shape.

    Args:
        location: The Location to fetch a property record for. Its own
            geocoded ``address`` (when already resolved - see
            ``services.locations.enrichment.AddressEnrichmentSource``) is passed
            through to REData as an additional search key; REData decides for
            itself whether/how to use it alongside anything it already knows.
        latitude: The latitude to look up - passed explicitly (rather than
            re-read off ``location``) so the panel path can use the pin's
            own effective marker coordinates, keeping the coordinates
            queried and the ``query_key`` recorded on the cache row in sync.
        longitude: The longitude to look up.

    Returns:
        ``{"available": True, ...record payload}`` on success, or
        ``{"available": False, "reason": ..., "message": ..., "links": {...}?}``
        - ``links`` (assessor/treasurer/recorder URLs) is present for the
        manual-lookup reasons (``manual_only``/CAPTCHA-``blocked``), carried
        on REData's error response so no second lookup round-trip is needed.

    Raises:
        PropertyRecordsUnavailableError: Only for a reason in
            ``TRANSIENT_REASONS`` (``source_error``, ``source_rate_limited``,
            ``rate_limited``) - a
            transient outage (REData itself, or a source it depends on) must
            not be written to the cache as a durable "no data" fact; the
            panel/enrichment frameworks' own failure handling retries it
            instead.
    """
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

    try:
        payload = RedataGateway().lookup_parcel(latitude, longitude, situs_address=location.address or "")
    except PropertyRecordsUnavailableError as exc:
        if exc.reason in TRANSIENT_REASONS:
            raise
        result: dict[str, Any] = {"available": False, "reason": exc.reason, "message": str(exc)}
        if exc.links:
            result["links"] = dict(exc.links)
        return result

    payload["available"] = True

    if payload.get("uuid"):
        parcel_uuid = payload["uuid"]
        gateway = RedataGateway()

        # Cheap local precheck for the two supplementary calls below that
        # *are* coverage-registry domains (assessments, sale_records) - see
        # RedataGateway.lookup_coverage. An optimization, not a dependency:
        # a failed precheck falls back to calling both unconditionally rather
        # than losing them, so a coverage-endpoint outage never blanks
        # sections the parcel actually has data for.
        try:
            coverage = gateway.lookup_coverage(parcel_uuid)
        except PropertyRecordsUnavailableError:
            coverage = {}

        # Supplementary assessor history (annual valuations; Cook County
        # today). Best-effort: the record card stands on its own, so a
        # failure or no-coverage answer here must not blank it - the history
        # simply reappears on the next refresh cycle.
        if _coverage_worth_calling(coverage, "assessments"):
            try:
                rows = gateway.lookup_assessments(parcel_uuid)
            except PropertyRecordsUnavailableError:
                rows = []
            history = _assessment_history(rows, payload.get("apn") or "")
            if history:
                payload["assessment_history"] = history

        # Supplementary recorded sales (CT OPM, Cook County) - same
        # best-effort stance. Matched rows are appended to sales_history so
        # the existing OFFICIAL-sale pipeline ingests them unchanged.
        if _coverage_worth_calling(coverage, "sale_records"):
            try:
                sale_rows = gateway.lookup_sale_records(parcel_uuid)
            except PropertyRecordsUnavailableError:
                sale_rows = []
            supplementary = _supplementary_sales(sale_rows, payload.get("situs_address") or "", payload.get("apn") or "")
            if supplementary:
                payload["sales_history"] = list(payload.get("sales_history") or []) + supplementary

        # Encumbrances and unpaid tax. For this application these are the most
        # telling records on the card: an open code-enforcement lien and years
        # of delinquent tax are what "abandoned" looks like in public records,
        # long before anything says so in words. Same best-effort stance as
        # above - the card stands without them. Neither is a coverage-registry
        # domain (see lookup_coverage's own docstring), so there is no cheap
        # way to know in advance whether either will return anything - these
        # stay unconditional.
        try:
            lien_rows = gateway.lookup_liens(parcel_uuid)
        except PropertyRecordsUnavailableError:
            lien_rows = []
        if lien_rows:
            payload["liens"] = _lien_rows(lien_rows)

        try:
            tax_rows = gateway.lookup_tax_payments(parcel_uuid)
        except PropertyRecordsUnavailableError:
            tax_rows = []
        if tax_rows:
            payload["tax_status"] = _tax_status(tax_rows)

        # Neighbourhood demographics (census tract population/income/home
        # value/rent/owner-renter split) - genuinely useful context for
        # someone researching a site. Best-effort: the endpoint 503s wholesale
        # without REData's own Census API key configured server-side, and that
        # is no different from any other supplementary source being down.
        try:
            demographics = gateway.lookup_demographics(parcel_uuid)
        except PropertyRecordsUnavailableError:
            demographics = None
        if demographics:
            payload["demographics"] = demographics

        # The park containing this parcel, if any - a real point-in-boundary
        # check, unlike plugins.builtin.nps's nearest-by-coordinate panel
        # elsewhere on the same pin (see that plugin's own docstring for the
        # precision tradeoff it accepts). nearby_parks duplicates that
        # existing panel, so it is read and discarded here rather than shown
        # twice.
        try:
            national_parks = gateway.lookup_national_parks(parcel_uuid)
        except PropertyRecordsUnavailableError:
            national_parks = {}
        if containing_park := national_parks.get("containing_park"):
            payload["containing_park"] = containing_park

    return payload


def _lien_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape lien rows for display, newest filing first.

    ``status`` is free text that publishers spell inconsistently, so it is
    passed through as a label rather than interpreted.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_liens`.

    Returns:
        Display rows carrying type, amount, filing date and status.
    """
    shaped = [
        {
            "lien_type": (row.get("lien_type") or "Lien").strip(),
            "amount": row.get("amount"),
            "filed_date": row.get("filed_date"),
            "status": (row.get("status") or "").strip(),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return sorted(shaped, key=lambda row: row.get("filed_date") or "", reverse=True)[:_MAX_LIEN_ROWS]


def _tax_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise tax history into the two facts worth showing.

    ``delinquent`` is the publisher's own determination and is not derived from
    ``paid``: a bill is unpaid before its due date without being delinquent, so
    counting unpaid rows as delinquency would overstate distress on a property
    whose current bill simply is not due yet.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_tax_payments`.

    Returns:
        The latest year on record and how many years are marked delinquent.
    """
    # Bound and narrowed in one place: testing `row.get(...)` in the condition and
    # reading it again in the value is two lookups that a reader - and mypy - has
    # to take on trust are the same answer.
    entries = [row for row in rows if isinstance(row, dict)]
    years = [year for row in entries if isinstance(year := row.get("tax_year"), int)]
    delinquent = sorted(year for row in entries if row.get("delinquent") and isinstance(year := row.get("tax_year"), int))
    return {
        "latest_year": max(years) if years else None,
        "delinquent_years": delinquent,
        "delinquent_count": len(delinquent),
    }


#: Every spelling a sale-record provider uses for "the parcel this sale was on",
#: most specific first. REData normalizes what it can onto promoted columns, but
#: a parcel number is the one identifier whose format is the publisher's own, so
#: it stays in the provider's raw ``attributes``.
_PARCEL_NUMBER_KEYS: tuple[str, ...] = ("pin", "parcel_identifier", "parcel_id")


def _supplementary_sales(rows: list[dict[str, Any]], situs_address: str, apn: str) -> list[dict[str, Any]]:
    """Sale rows attributable to *this* parcel, shaped for the sales_history pipeline.

    The endpoint answers for parcels *near* the coordinate and links no row to
    a parcel, so attribution is on us: a row counts only when its
    ``situs_address`` equals the record's own (compared with punctuation and
    case stripped), or its ``attributes`` carry a parcel number matching the
    record's APN. An unmatched row is a neighbour's sale and is dropped -
    misattributing one would be worse than missing it.

    Each provider spells that parcel number differently, and a spelling this
    function does not know is silently a whole state with no sale history:
    Florida's statewide DOR layer publishes no ``situs_address`` at all and
    keys its parcel under ``parcel_id``, so before that key was read here every
    Florida sale was dropped and the card looked like REData had no coverage.
    :data:`_PARCEL_NUMBER_KEYS` is therefore the place to add a new provider.

    Rows the county itself marks as unrepresentative
    (``attributes.arms_length`` explicitly false - bundle sales, nominal
    transfers) are excluded: their ``sale_price`` is not this parcel's market
    price, and the sales pipeline has no way to carry the caveat.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_sale_records`.
        situs_address: The record payload's own street address, possibly blank.
        apn: The record payload's own parcel number, possibly blank.

    Returns:
        ``{"date", "price", "grantor", "grantee"}`` dicts (the shape
        ``_write_official_owners_and_sales`` reads; these providers publish no
        party names, so grantor/grantee are blank), oldest first.
    """

    def normalize(value: str) -> str:
        return "".join(ch for ch in value if ch.isalnum()).casefold()

    our_address = normalize(situs_address)
    our_apn = normalize(apn)

    matched: list[dict[str, Any]] = []
    for row in rows:
        attributes = row.get("attributes") or {}
        if attributes.get("arms_length") is False:
            continue
        row_address = normalize(str(row.get("situs_address") or ""))
        row_pin = normalize(next((str(attributes[key]) for key in _PARCEL_NUMBER_KEYS if attributes.get(key)), ""))
        address_match = bool(our_address) and row_address == our_address
        pin_match = bool(our_apn) and row_pin == our_apn
        if not (address_match or pin_match):
            continue
        if not (row.get("sale_date") or row.get("sale_price")):
            continue
        matched.append({"date": row.get("sale_date") or "", "price": row.get("sale_price") or "", "grantor": "", "grantee": ""})

    matched.sort(key=lambda sale: sale["date"] or "")
    return matched


def _assessment_history(rows: list[dict[str, Any]], apn: str) -> list[dict[str, Any]]:
    """One parcel's assessment rows, newest tax year first.

    The endpoint answers for parcels *near* the coordinate, so this filters to
    ours: rows matching the record's own APN when it is known (compared with
    punctuation stripped - assessors and GIS vendors format the same PIN
    differently), else the identifier with the most rows, which for a
    parcel-centred query is the parcel itself.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_assessments`.
        apn: The record payload's own parcel number, possibly blank.

    Returns:
        Compact ``{"tax_year", "total_value", "value_stage"}`` dicts, capped
        at ten years.
    """

    def normalize(value: str) -> str:
        return "".join(ch for ch in value if ch.isalnum()).casefold()

    keyed: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        identifier = normalize(str(row.get("parcel_identifier") or ""))
        if identifier:
            keyed.setdefault(identifier, []).append(row)
    if not keyed:
        return []

    if apn:
        # A known APN that matches no row means *our* parcel has no coverage -
        # falling back to another identifier would display a neighbour's
        # valuations under this card.
        ours = keyed.get(normalize(apn))
        if ours is None:
            return []
    else:
        ours = max(keyed.values(), key=len)

    ours = [row for row in ours if row.get("total_value")]
    ours.sort(key=lambda row: row.get("tax_year") or 0, reverse=True)
    return [{"tax_year": row.get("tax_year"), "total_value": row["total_value"], "value_stage": row.get("value_stage") or ""} for row in ours[:10]]


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
    owner's link, since a single automated snapshot isn't a trustworthy enough
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


#: Recorded-document links shown before the list is truncated.
_MAX_DEED_LINKS = 5

#: Human-readable labels for BuildingCharacteristics fields, in display order.
_BUILDING_CHARACTERISTIC_LABELS: tuple[tuple[str, str], ...] = (
    ("stories", "Stories"),
    ("roof_material", "Roof"),
    ("wall_material", "Exterior walls"),
    ("garage", "Garage"),
    ("heating_type", "Heating"),
    ("quality", "Building quality"),
    ("condition", "Building condition"),
)


#: The Census Bureau's four Special Land Use Area categories, in the order this
#: app cares about them: whether the ground you would be standing on is
#: access-controlled comes before what it is called.
#:
#: REData resolves these on every parcel fetch (a point-in-polygon test against
#: TIGERweb's Special Land Use Areas layer) and UrbanLens has been caching the
#: answer and showing none of it. For this application that is the single most
#: consequential field on the record: a site inside a military installation or a
#: correctional facility is not a legal question about trespass, it is a
#: different statute, and "the parcel record was fetched and it did say so" is
#: not a good place for that to have been left unread.
_SPECIAL_LAND_USE_LABELS: tuple[tuple[str, str], ...] = (
    ("military_installation", "Military installation"),
    ("correctional_facility", "Correctional facility"),
    ("national_park", "National park"),
    ("college_university", "College or university"),
)


def special_land_use_rows(areas: Any) -> list[dict[str, str]]:
    """Name the Special Land Use Areas a parcel falls inside.

    Args:
        areas: REData's ``special_land_use_areas`` mapping - keyed by category,
            each value ``{"name": ..., "geoid": ...}`` or ``None``. ``{}`` (the
            common case) means the parcel is inside none of them.

    Returns:
        ``{"category", "label", "name"}`` dicts in :data:`_SPECIAL_LAND_USE_LABELS`
        order, skipping categories the parcel is not inside. A category present
        but unnamed still yields a row - *that* the parcel is inside a
        correctional facility matters whether or not the layer says which one.
    """
    if not isinstance(areas, dict):
        return []

    rows: list[dict[str, str]] = []
    for category, label in _SPECIAL_LAND_USE_LABELS:
        area = areas.get(category)
        if not area:
            continue
        name = str(area.get("name") or "").strip() if isinstance(area, dict) else ""
        rows.append({"category": category, "label": label, "name": name or label})
    return rows


def _demographic_number(value: Any) -> float | None:
    """Parse one of REData's demographics fields (a decimal string, per the ACS API) to a float."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _may_see_nearby_research(user: Any) -> bool:
    """Whether this user may see this panel's nearby-area (not-the-parcel-itself) data.

    Currently gates only the neighbourhood demographics section - see
    :func:`_demographics_rows` and the module docstring.

    Args:
        user: The viewing user (``services.property.owner_access.viewer_of(pin)``),
            or None for a caller with no viewer to resolve - fails closed,
            same reasoning as ``can_see_official_owners``.

    Returns:
        True when the user holds ``SiteFeature.NEARBY_RESEARCH``.
    """
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if user is None:
        return False
    return user_has_feature(user, SiteFeature.NEARBY_RESEARCH)


def _demographics_rows(demographics: Any, *, show_demographics: bool) -> list[dict[str, str]]:
    """Neighbourhood context from the parcel's census tract, as display rows.

    REData has been resolving this on every parcel fetch (see
    :meth:`RedataGateway.lookup_demographics`) and this app has shown none of
    it - genuinely useful context for someone researching a site, distinct
    from the parcel's own facts above it.

    Args:
        demographics: :meth:`RedataGateway.lookup_demographics`'s payload, or
            None/anything falsy (no coordinate, outside the USA, or the
            endpoint was unavailable - see ``_fetch_payload``'s best-effort
            handling of it).
        show_demographics: See :func:`_may_see_nearby_research` - False
            returns ``[]`` unconditionally, without even reading ``demographics``.

    Returns:
        Display rows for population, median household income, median home
        value, median rent, and the owner/renter split - omitting any field
        the ACS estimate doesn't carry.
    """
    if not show_demographics or not isinstance(demographics, dict):
        return []

    rows: list[dict[str, str]] = []
    if (population := _demographic_number(demographics.get("population"))) is not None:
        rows.append({"label": "Neighborhood population", "value": f"{population:,.0f}"})
    if (income := _demographic_number(demographics.get("median_household_income"))) is not None:
        rows.append({"label": "Median household income", "value": f"${income:,.0f}"})
    if (home_value := _demographic_number(demographics.get("median_home_value"))) is not None:
        rows.append({"label": "Median home value", "value": f"${home_value:,.0f}"})
    if (rent := _demographic_number(demographics.get("median_gross_rent"))) is not None:
        rows.append({"label": "Median gross rent", "value": f"${rent:,.0f}/mo"})
    owner_pct = _demographic_number(demographics.get("percent_owner_occupied"))
    renter_pct = _demographic_number(demographics.get("percent_renter_occupied"))
    if owner_pct is not None and renter_pct is not None:
        rows.append({"label": "Owner/renter occupied", "value": f"{owner_pct:.0f}% / {renter_pct:.0f}%"})
    return rows


def _render_available(data: dict[str, Any], *, show_owner: bool, show_demographics: bool) -> dict[str, Any]:
    """Build the info-panel context for a successful record.

    Args:
        data: The cached property-record payload.
        show_owner: Whether this viewer may see the owner's name. County
            assessor data is the paid half of this card - the parcel/tax
            facts stay unconditional, the private individual's name does not
            (see ``services.property.owner_access``).
        show_demographics: Whether this viewer may see the neighbourhood
            demographics section - see :func:`_may_see_nearby_research` and
            the module docstring.
    """
    meta = [{"label": "Address", "value": data["situs_address"]}] if data.get("situs_address") else []
    if data.get("apn"):
        meta.append({"label": "APN / Parcel ID", "value": data["apn"]})
    if data.get("prior_parcel_ids"):
        meta.append({"label": "Prior parcel ID", "value": ", ".join(data["prior_parcel_ids"])})
    if data.get("land_use_code"):
        meta.append({"label": "Land use", "value": data["land_use_code"]})
    if data.get("zoning_code"):
        meta.append({"label": "Zoning", "value": data["zoning_code"]})
    if data.get("subdivision_name"):
        meta.append({"label": "Subdivision", "value": data["subdivision_name"]})
    if data.get("neighborhood"):
        meta.append({"label": "Neighborhood", "value": data["neighborhood"]})
    if data.get("lot_size_sqft"):
        meta.append({"label": "Lot size", "value": f"{data['lot_size_sqft']:,.0f} sq ft"})
    if data.get("building_sqft"):
        meta.append({"label": "Building size", "value": f"{data['building_sqft']:,.0f} sq ft"})
    if data.get("year_built"):
        meta.append({"label": "Year built", "value": data["year_built"]})
    for area in special_land_use_rows(data.get("special_land_use_areas")):
        meta.append({"label": area["label"], "value": area["name"]})
    if data.get("flood_zone_code"):
        meta.append({"label": "Flood zone", "value": data["flood_zone_code"]})

    building = data.get("building_characteristics") or {}
    for field_name, label in _BUILDING_CHARACTERISTIC_LABELS:
        value = building.get(field_name)
        if value:
            meta.append({"label": label, "value": f"{value:g}" if field_name == "stories" else value})
    if building.get("building_count") and building["building_count"] > 1:
        meta.append({"label": "Buildings on parcel", "value": building["building_count"]})

    assessed = data.get("assessed_value") or {}
    if assessed.get("total"):
        year_suffix = f" ({assessed['year']})" if assessed.get("year") else ""
        meta.append({"label": f"Assessed value{year_suffix}", "value": f"${assessed['total']:,.0f}"})
    for row in (data.get("assessment_history") or [])[:5]:
        # An assessed value is a statutory fraction of market value; the
        # stage matters because a Board of Review figure is post-appeal.
        stage_suffix = f" ({row['value_stage']})" if row.get("value_stage") else ""
        meta.append({"label": f"Assessed {row.get('tax_year') or '?'}", "value": f"${row['total_value']:,.0f}{stage_suffix}"})

    # Distress signals, last because they are the conclusion the rows above
    # lead to rather than another attribute of the building.
    tax_status = data.get("tax_status") or {}
    if tax_status.get("delinquent_count"):
        years = tax_status.get("delinquent_years") or []
        span = f"{years[0]}-{years[-1]}" if len(years) > 1 else str(years[0])
        meta.append({"label": "Tax delinquent", "value": f"{tax_status['delinquent_count']} year{'s' if tax_status['delinquent_count'] != 1 else ''} ({span})"})
    elif tax_status.get("latest_year"):
        meta.append({"label": "Tax status", "value": f"Current through {tax_status['latest_year']}"})

    for lien in (data.get("liens") or [])[:5]:
        amount = f"${float(lien['amount']):,.0f}" if lien.get("amount") not in (None, "") else ""
        status = f" - {lien['status']}" if lien.get("status") else ""
        filed = f" (filed {lien['filed_date']})" if lien.get("filed_date") else ""
        meta.append({"label": lien["lien_type"].title(), "value": f"{amount}{status}{filed}".strip(" -")})
    if data.get("market_value"):
        meta.append({"label": "Market value", "value": f"${data['market_value']:,.0f}"})
    if building.get("outbuilding_value"):
        meta.append({"label": "Outbuilding value", "value": f"${building['outbuilding_value']:,.0f}"})
    if data.get("exemption_type"):
        exempt_suffix = f" (${data['deferred_value']:,.0f} deferred)" if data.get("deferred_value") else ""
        meta.append({"label": "Exemption", "value": f"{data['exemption_type']}{exempt_suffix}"})
    if data.get("tax_district"):
        meta.append({"label": "Tax district", "value": data["tax_district"]})
    if data.get("school_district"):
        meta.append({"label": "School district", "value": data["school_district"]})

    # Neighbourhood demographics (the parcel's census tract) - context about
    # the area, not the parcel itself, so it sits after the parcel's own tax
    # geography rather than among the parcel facts above it.
    meta.extend(_demographics_rows(data.get("demographics"), show_demographics=show_demographics))

    # Recorded-document references (deeds, plats). Linked rather than listed as
    # bare URLs: they are the primary sources behind the ownership history above,
    # and a recorder's URL is not text anyone reads. Capped because a
    # long-subdivided parcel can carry dozens.
    document_links = [link.strip() for link in (data.get("deed_document_links") or []) if isinstance(link, str) and link.strip()]
    for index, link in enumerate(document_links[:_MAX_DEED_LINKS], start=1):
        # Numbered by displayed position, not by position in the source list -
        # a county that publishes blanks between real entries would otherwise
        # produce "Recorded document 2, Recorded document 5".
        meta.append({"label": "Recorded document" if index == 1 else f"Recorded document {index}", "value": "View document", "href": link})

    chips: list[str] = []
    # First, because it is the one fact here that changes what a visit *is*
    # rather than describing the property.
    chips.extend(area["label"] for area in special_land_use_rows(data.get("special_land_use_areas")))
    # A real point-in-boundary check (see RedataGateway.lookup_national_parks),
    # not the nearest-by-coordinate answer plugins.builtin.nps shows elsewhere
    # on this same pin - same rationale as the Special Land Use chips above:
    # it changes what a visit is, not just describes the property.
    containing_park = data.get("containing_park") or {}
    if full_name := containing_park.get("full_name"):
        chips.append(f"Situated within {full_name}")
    if data.get("field_mismatches"):
        chips.append("Sources disagree")
    if any(entry.get("delinquent") for entry in data.get("tax_history") or []):
        chips.append("Delinquent taxes")
    if data.get("parcel_geometry"):
        chips.append("Boundary available")

    # footer_link = {"url": data["source"]["url"], "label": f"View on {data['source']['provider']}"} if data["source"].get("url") else None

    owner_names = data.get("owner_name") or []
    if owner_names and not show_owner:
        # Named rather than silently dropped: "this parcel has a recorded
        # owner you can't see" is a different (and honest) statement from
        # "no owner on record", and the second would read as missing data.
        chips.append("Owner on record - subscribers only")

    return {
        "heading_name": (", ".join(owner_names) or None) if show_owner else None,
        "chips": chips,
        "meta": meta,
    }


def _render_manual_only(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build the info-panel context for the "a human must look this up" cases (manual-only, CAPTCHA-blocked)."""
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
    """County property ownership/tax record card on the Private Pin page."""

    key = "property_records"
    cache_source = _CACHE_SOURCE
    section_id = "property-records-section"
    icon = "home_work"
    title = "Property Records"
    # Deliberately not exposed on the external API: this is ownership/tax
    # record data pulled from county GIS/tax sources, and redistributing it
    # through a bearer-key API is a different (and more sensitive) exposure
    # than showing it to a logged-in user on their own pin page. Opt back in
    # only after that's been explicitly reviewed.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset()

    def fetch(self, pin: Pin) -> None:
        """Fetch (or reuse the enrichment source's cached fetch of) this pin's property record."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        payload = _fetch_payload(pin.location, lat, lng)
        LocationCache.set(pin.location, self.cache_source, payload, query_key=f"{lat:.5f},{lng:.5f}")
        if payload.get("available"):
            _write_official_owners_and_sales(pin.location, payload)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Render the found record, the manual-lookup pointer card, or nothing (204).

        The owner's name and the demographics section are each shown only to
        a viewer entitled to them - see ``services.property.owner_access.viewer_of``
        for who that is, and why an unresolvable viewer withholds both rather
        than showing them.
        """
        from urbanlens.dashboard.services.property.owner_access import can_see_official_owners, viewer_of

        if not data:
            return None
        if data.get("available"):
            viewer = viewer_of(pin)
            return _render_available(data, show_owner=can_see_official_owners(viewer), show_demographics=_may_see_nearby_research(viewer))
        if data.get("reason") in (REASON_MANUAL_ONLY, REASON_BLOCKED):
            return _render_manual_only(data)
        return None

    def debug_count(self, data: dict) -> int:
        """1 when a record (or a manual-lookup pointer) was found, else 0."""
        return 1 if (data or {}).get("available") or (data or {}).get("reason") in (REASON_MANUAL_ONLY, REASON_BLOCKED) else 0


class PropertyRecordsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the property-records cache (and OFFICIAL owner/sale rows) per Location."""

    key: ClassVar[str] = "property_records"
    verbose_name: ClassVar[str] = "Property Records (county GIS/tax data)"
    cache_source: ClassVar[str] = _CACHE_SOURCE
    service_keys: ClassVar[tuple[str, ...]] = ("redata_api",)
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self) -> bool:
        """Requires REData to be configured - this source has no other backend.

        Without it the cycle picks candidates, every fetch raises, and the run
        logs one exception per location. Answering here skips the source for
        the whole cycle instead, which is what "unavailable" means to
        ``self_reported_skip``.
        """
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return redata_configured()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Call REData and, on success, upsert OFFICIAL owner/sale rows.

        Args:
            location: The location to fetch a property record for.

        Returns:
            Tuple of (payload, coordinate query key) - the base class persists
            ``payload`` to the shared ``LocationCache`` row.

        Raises:
            PropertyRecordsUnavailableError: For a transient source outage -
                the enrichment runner logs it and retries the location on a
                later cycle instead of marking it done.
        """
        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        payload = _fetch_payload(location, lat, lng)
        if payload.get("available"):
            _write_official_owners_and_sales(location, payload)
        return payload, f"{lat:.5f},{lng:.5f}"


class PropertyRecordsPlugin(UrbanLensPlugin):
    """US county property ownership & tax record retrieval, via REData. USA only."""

    name: ClassVar[str] = "property_records"
    verbose_name: ClassVar[str] = "Property Records"
    description: ClassVar[str] = (
        "Parcel ownership, assessed value, and sale history lookups, retrieved from REData, a standalone "
        "service. Populates the pin/wiki Ownership and Sale History cards with OFFICIAL-sourced records and "
        "shows a details card on the Private Pin page. Coverage varies by county - a place REData doesn't yet "
        "have data for surfaces as 'not automatable' rather than failing silently. USA only. Requires "
        "UL_REDATA_API_URL/UL_REDATA_API_KEY to be configured."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's own external API.

        Generous relative to the free third-party budgets this plugin used to
        declare (census_geocoder/property_records_gis/property_records_scrape)
        - REData is our own service, not a shared public API, and it does its
        own internal per-host pacing against the underlying county sources.
        """
        return {
            "redata_api": ServiceDefaults(
                display_name="REData (property records service)",
                calls_per_minute=120,
                calls_per_day=10000,
                usa_only=True,
                notes="Our own standalone property-records service - not a third-party budget, just a sanity ceiling.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the pin-detail Property Records card."""
        return [PropertyRecordsPanelSource()]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute background-fill of property records for every pinned Location."""
        return [PropertyRecordsEnrichmentSource()]
