"""Property records plugin: US county property ownership & tax data via REData."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import logging
from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.property_records.redata_gateway import REASON_BLOCKED, REASON_MANUAL_ONLY
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.pins.external_data import CoordinateGatedInfoPanelSource, OverviewSummary, PanelApiKind, PanelPlacement
from urbanlens.dashboard.services.pins.redata_panel import RedataBackedSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.property_owner.model import WikiOwner
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.pins.external_data import PanelSource

logger = logging.getLogger(__name__)

_CACHE_SOURCE = "property_records"

#: Said in place of an official owner's name to a viewer not entitled to it.
OWNER_WITHHELD = "Owner on record - subscribers only"


#: Liens shown on the card. A parcel with a long enforcement history is
#: interesting, but the card is a summary - the full list belongs to whoever
#: goes looking in the county records.
_MAX_LIEN_ROWS = 8


def _coverage_worth_calling(coverage: dict[str, Any], domain: str) -> bool:
    """Whether a coverage-precheck domain is worth an actual supplementary call.

    Args:
        coverage: :meth:`RedataGateway.lookup_coverage`'s payload - ``{}`` both for a parcel with no coverage data and for a failed precheck (see :func:`_fetch_payload`), which is why this defaults to calling rather than skipping.
        domain: The coverage key to check (e.g. ``"assessments"``, ``"sale_records"``).

    Returns:
        False only when ``coverage[domain]["available"]`` is exactly ``False``."""
    entry = coverage.get(domain)
    return not (isinstance(entry, dict) and entry.get("available") is False)


def _fetch_payload(location: Location, latitude: float, longitude: float) -> dict[str, Any]:
    """Call REData and return the shared LocationCache payload shape.

    Args:
        location: The Location to fetch a property record for.
        latitude: The latitude to look up - passed explicitly (rather than re-read off ``location``) so the panel path can use the pin's own effective marker coordinates, keeping the coordinates queried and the ``query_key`` recorded on the cache row in sync.
        longitude: The longitude to look up.

    Returns:
        ``{"available": True, ...record payload}`` on success, or ``{"available": False, "reason": ..., "message": ..., "links": {...}?}`` - ``links`` (assessor/treasurer/recorder URLs) is present for the manual-lookup reasons...

    Raises:
        PropertyRecordsUnavailableError: Only when nothing was learned (``is_outage``: REData or a source it depends on is down, or REData said to ask again later) - that must not be written to the cache as a durable "no data" fact."""
    from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError, RedataGateway

    try:
        payload = RedataGateway().lookup_parcel(latitude, longitude, situs_address=location.address or "")
    except PropertyRecordsUnavailableError as exc:
        if exc.is_outage:
            raise
        result: dict[str, Any] = {"available": False, "reason": exc.reason, "message": str(exc)}
        if exc.links:
            result["links"] = dict(exc.links)
        return result

    payload["available"] = True

    if payload.get("uuid"):
        parcel_uuid = payload["uuid"]
        gateway = RedataGateway()

        # Cheap local precheck for the two supplementary calls below that *are* coverage-registry
        # domains (assessments, sale_records) - see RedataGateway.lookup_coverage.
        try:
            coverage = gateway.lookup_coverage(parcel_uuid)
        except PropertyRecordsUnavailableError:
            coverage = {}

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

        # Encumbrances and unpaid tax.
        # For this application these are the most telling records on the card: an open
        # code-enforcement lien and years of delinquent tax are what "abandoned" looks like in
        # public records, long before anything says so in words.
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

        # The record names only today's owner; REData's owner rows add former owners, contact details and how
        # many other parcels each holds, and its recorded sales outlast the one retrieval behind this record.
        try:
            owner_rows = gateway.lookup_owners(parcel_uuid)
        except PropertyRecordsUnavailableError:
            owner_rows = []
        if owners := _owner_records(owner_rows):
            payload["owners"] = owners

        try:
            recorded_sales = gateway.lookup_sales(parcel_uuid)
        except PropertyRecordsUnavailableError:
            recorded_sales = []
        if recorded := _recorded_sales(recorded_sales):
            payload["sales_history"] = _merged_sales(payload.get("sales_history") or [], recorded)

        # Neighbourhood demographics (census tract population/income/home value/rent/owner-renter
        # split) - genuinely useful context for someone researching a site.
        # Best-effort: the endpoint 503s wholesale without REData's own Census API key configured
        # server-side, and that is no different from any other supplementary source being down.
        try:
            demographics = gateway.lookup_demographics(parcel_uuid)
        except PropertyRecordsUnavailableError:
            demographics = None
        if demographics:
            payload["demographics"] = demographics

        # The park containing this parcel, if any - a real point-in-boundary check, unlike
        # plugins.builtin.nps's nearest-by-coordinate panel elsewhere on the same pin (see that
        # plugin's own docstring for the precision tradeoff it accepts). nearby_parks duplicates
        # that existing panel, so it is read and discarded here rather than shown twice.
        try:
            national_parks = gateway.lookup_national_parks(parcel_uuid)
        except PropertyRecordsUnavailableError:
            national_parks = {}
        if containing_park := national_parks.get("containing_park"):
            payload["containing_park"] = containing_park

    return payload


def _lien_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape lien rows for display, newest filing first.
    ``status`` is free text that publishers spell inconsistently, so it is passed through as a label rather than interpreted.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_liens`.

    Returns:
        Display rows carrying type, amount, filing date and status."""
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

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_tax_payments`.

    Returns:
        The latest year on record and how many years are marked delinquent."""
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


#: Every spelling a sale-record provider uses for "the parcel this sale was on", most specific first.
#: REData normalizes what it can onto promoted columns, but a parcel number is the one identifier
#: whose format is the publisher's own, so it stays in the provider's raw ``attributes``.
_PARCEL_NUMBER_KEYS: tuple[str, ...] = ("pin", "parcel_identifier", "parcel_id")


def _supplementary_sales(rows: list[dict[str, Any]], situs_address: str, apn: str) -> list[dict[str, Any]]:
    """Sale rows attributable to *this* parcel, shaped for the sales_history pipeline.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_sale_records`.
        situs_address: The record payload's own street address, possibly blank.
        apn: The record payload's own parcel number, possibly blank.

    Returns:
        ``{"date", "price", "grantor", "grantee"}`` dicts (the shape ``_write_official_owners_and_sales`` reads; these providers publish no party names, so grantor/grantee are blank), oldest first."""

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

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_assessments`.
        apn: The record payload's own parcel number, possibly blank.

    Returns:
        Compact ``{"tax_year", "total_value", "value_stage"}`` dicts, capped at ten years."""

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

    valued = [(row, total) for row in ours if (total := _decimal_number(row.get("total_value")))]
    valued.sort(key=lambda pair: pair[0].get("tax_year") or 0, reverse=True)
    return [{"tax_year": row.get("tax_year"), "total_value": total, "value_stage": row.get("value_stage") or ""} for row, total in valued[:10]]


#: REData owner fields an official owner row is kept current from, keyed by the row's own field.
_OWNER_CONTACT_FIELDS: tuple[tuple[str, str], ...] = (
    ("company_name", "company_name"),
    ("address", "mailing_address"),
    ("care_of", "care_of"),
    ("phone", "phone"),
    ("email", "email"),
)


def _owner_records(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shape REData's owner rows for the cache, leaving out rows another REData client typed in.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_owners`.

    Returns:
        ``{"name", contact fields, "first_observed", "last_observed", "current", "other_parcels"}`` dicts. ``current``
        is None when the parcel has no official record to judge by.
    """
    records: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("source") == "manual":
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        parcels = {parcel for parcel in row.get("parcels") or [] if isinstance(parcel, int | str)}
        current = row.get("current")
        record: dict[str, Any] = {redata: str(row.get(redata) or "").strip() for _, redata in _OWNER_CONTACT_FIELDS}
        record.update(
            name=name,
            first_observed=str(row.get("first_observed_at") or ""),
            last_observed=str(row.get("last_observed_at") or ""),
            current=current if isinstance(current, bool) else None,
            other_parcels=max(len(parcels) - 1, 0),
        )
        records.append(record)
    return records


def _recorded_sales(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """REData's recorded sales of the parcel, shaped like the record's own ``sales_history``.

    Args:
        rows: Raw rows from :meth:`RedataGateway.lookup_sales`.

    Returns:
        ``{"date", "price", "grantor", "grantee", "doc_type", "doc_number"}`` dicts, leaving out client-entered rows and
        rows with neither a date nor a price.
    """
    sales: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("source") == "manual":
            continue
        if not (row.get("sale_date") or row.get("sale_price")):
            continue
        sale: dict[str, Any] = {"date": row.get("sale_date") or "", "price": row.get("sale_price") or ""}
        sale.update({key: str(row.get(key) or "").strip() for key in ("grantor", "grantee", "doc_type", "doc_number")})
        sales.append(sale)
    return sales


def _merged_sales(known: list[dict[str, Any]], recorded: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The record's sales with REData's recorded ones added, one entry per date and price.

    Args:
        known: The record's ``sales_history`` so far.
        recorded: :func:`_recorded_sales`'s rows.

    Returns:
        ``known`` with blank party and document fields filled from a recorded sale of the same date and price, then
        the recorded sales it lacked.
    """

    def key(sale: dict[str, Any]) -> tuple[str, Decimal | None]:
        return str(sale.get("date") or ""), _parse_sale_price(sale.get("price"))

    merged = [dict(sale) for sale in known if isinstance(sale, dict)]
    by_key = {key(sale): sale for sale in merged}
    for sale in recorded:
        match = by_key.get(key(sale))
        if match is None:
            merged.append(sale)
            by_key[key(sale)] = sale
            continue
        for key_name in ("grantor", "grantee", "doc_type", "doc_number"):
            if not match.get(key_name) and sale.get(key_name):
                match[key_name] = sale[key_name]
    return merged


@dataclass(frozen=True)
class _OwnerDetails:
    """What one record says about one owner."""

    name: str
    contact: dict[str, str] = field(default_factory=dict)


def _fit(field_name: str, value: str) -> str:
    """``value`` cut to the owner field's length, so one overlong county value cannot fail the whole write."""
    from django.db.models import Field

    from urbanlens.dashboard.models.property_owner.model import WikiOwner

    model_field = WikiOwner._meta.get_field(field_name)  # noqa: SLF001 - fields are only exposed through _meta
    max_length = model_field.max_length if isinstance(model_field, Field) else None
    return value[:max_length] if max_length else value


def _current_owner_details(payload: dict[str, Any]) -> list[_OwnerDetails]:
    """The owners the record names, each with the contact details known for it.

    Args:
        payload: A successful ``_fetch_payload`` result.

    Returns:
        One entry per distinct name in ``owner_name`` (else per REData owner marked current). The record's own mailing
        address and care-of line come first, being from the newest retrieval; REData's owner row fills the rest.
    """
    owners = [owner for owner in payload.get("owners") or [] if isinstance(owner, dict) and owner.get("name")]
    by_name = {str(owner["name"]).casefold(): owner for owner in owners}
    names = [str(name).strip() for name in payload.get("owner_name") or [] if str(name or "").strip()]
    if not names:
        names = [str(owner["name"]) for owner in owners if owner.get("current") is True]
    record_contact = {"address": str(payload.get("owner_mailing_address") or "").strip(), "care_of": str(payload.get("owner_care_of") or "").strip()}

    details: list[_OwnerDetails] = []
    for name in dict.fromkeys(names, None):
        known = by_name.get(name.casefold()) or {}
        contact = {own: record_contact.get(own) or str(known.get(redata) or "").strip() for own, redata in _OWNER_CONTACT_FIELDS}
        details.append(_OwnerDetails(name=name, contact={own: _fit(own, value) for own, value in contact.items() if value}))
    return details


def _owner_named(location: Location, name: str) -> WikiOwner:
    """The owner of this name a location already knows, else a new OFFICIAL row, unlinked.

    Args:
        location: The location the record is for.
        name: A non-blank owner name, already cut to the field's length.

    Returns:
        A current owner of the location, else a party to one of its sales, else the new row.
    """
    from django.db.models import Q

    from urbanlens.dashboard.models.property_owner.meta import OwnerSource
    from urbanlens.dashboard.models.property_owner.model import WikiOwner

    existing = WikiOwner.objects.for_location(location).filter(name__iexact=name).first()
    if existing is None:
        parties = Q(sales_as_previous_owner__location=location) | Q(sales_as_new_owner__location=location)
        existing = WikiOwner.objects.filter(parties, name__iexact=name).first()
    return existing or WikiOwner.objects.create(name=name, source=OwnerSource.OFFICIAL)


def _sale_party_owner(location: Location, name: str) -> WikiOwner | None:
    """The owner row a sale of this location names; a party to a sale is not made a current owner.

    Args:
        location: The location sold.
        name: The party's name, as the record spells it.

    Returns:
        The party's row, or None for a blank name.
    """
    clean_name = _fit("name", (name or "").strip())
    return _owner_named(location, clean_name) if clean_name else None


def _current_official_owner(location: Location, details: _OwnerDetails) -> WikiOwner:
    """Link the named owner to the location, keeping an official row's contact details current.

    A member-entered owner of the same name is linked as it stands, never rewritten. A blank in the record never
    clears a known value.

    Args:
        location: The location the record is for.
        details: The record's owner.

    Returns:
        The linked owner.
    """
    from urbanlens.dashboard.models.property_owner.meta import OwnerSource

    owner = _owner_named(location, _fit("name", details.name))
    if owner.source == OwnerSource.OFFICIAL:
        changed = [field_name for field_name, value in details.contact.items() if getattr(owner, field_name) != value]
        for field_name in changed:
            setattr(owner, field_name, details.contact[field_name])
        if changed:
            owner.save(update_fields=[*changed, "updated"])
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

    A location's linked owners are its current ones, as the community Sale History form treats them: the record's
    owners are linked, an official owner it no longer names is unlinked, and a sale's parties are recorded on the sale
    only. A record naming no owner changes nobody.

    Args:
        location: The Location the record belongs to.
        payload: A successful (``available: True``) ``_fetch_payload`` result."""
    from django.db import transaction

    from urbanlens.dashboard.models.property_owner.meta import OwnerSource
    from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale

    with transaction.atomic():
        current = [_current_official_owner(location, details) for details in _current_owner_details(payload)]
        if current:
            superseded = WikiOwner.objects.for_location(location).filter(source=OwnerSource.OFFICIAL).exclude(pk__in=[owner.pk for owner in current])
            location.owners.remove(*superseded)

        for sale in payload.get("sales_history") or []:
            raw_date = sale.get("date")
            try:
                sale_date = date.fromisoformat(raw_date) if raw_date else None
            except ValueError:
                sale_date = None
            sale_price = _parse_sale_price(sale.get("price"))
            if sale_date is None and sale_price is None:
                continue

            recorded = WikiPropertySale.objects.for_location(location).filter(sale_date=sale_date, sale_price=sale_price).first()
            if recorded is None:
                recorded = WikiPropertySale.objects.create(location=location, source=OwnerSource.OFFICIAL, sale_date=sale_date, sale_price=sale_price)
            elif recorded.source != OwnerSource.OFFICIAL:
                continue
            # A sale first recorded from a source without party names takes them from a later one that has them.
            for side, party_name in ((recorded.previous_owners, sale.get("grantor")), (recorded.new_owners, sale.get("grantee"))):
                if side.exists():
                    continue
                party = _sale_party_owner(location, party_name or "")
                if party is not None:
                    side.add(party)


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


#: The Census Bureau's four Special Land Use Area categories, in the order this app cares about them:
#: whether the ground you would be standing on is access-controlled comes before what it is called.
#: REData resolves these on every parcel fetch (a point-in-polygon test against TIGERweb's Special
#: Land Use Areas layer) and UrbanLens has been caching the answer and showing none of it.
_SPECIAL_LAND_USE_LABELS: tuple[tuple[str, str], ...] = (
    ("military_installation", "Military installation"),
    ("correctional_facility", "Correctional facility"),
    ("national_park", "National park"),
    ("college_university", "College or university"),
)


def special_land_use_rows(areas: Any) -> list[dict[str, str]]:
    """Name the Special Land Use Areas a parcel falls inside.

    Args:
        areas: REData's ``special_land_use_areas`` mapping - keyed by category, each value ``{"name": ..., "geoid": ...}`` or ``None``.

    Returns:
        ``{"category", "label", "name"}`` dicts in :data:`_SPECIAL_LAND_USE_LABELS` order, skipping categories the parcel is not inside."""
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


def _decimal_number(value: Any) -> float | None:
    """Parse a REData numeric field that may arrive as a decimal string (demographics, assessments) to a float."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _may_see_nearby_research(user: Any) -> bool:
    """Whether this user may see this panel's nearby-area (not-the-parcel-itself) data.
    Currently gates only the neighbourhood demographics section - see :func:`_demographics_rows` and the module docstring.

    Args:
        user: The viewing user (``services.property.owner_access.viewer_of(pin)``), or None for a caller with no viewer to resolve - fails closed, same reasoning as ``can_see_official_owners``.

    Returns:
        True when the user holds ``SiteFeature.NEARBY_RESEARCH``."""
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if user is None:
        return False
    return user_has_feature(user, SiteFeature.NEARBY_RESEARCH)


def _demographics_rows(demographics: Any, *, show_demographics: bool) -> list[dict[str, str]]:
    """Neighbourhood context from the parcel's census tract, as display rows.

    Args:
        demographics: :meth:`RedataGateway.lookup_demographics`'s payload, or None/anything falsy (no coordinate, outside the USA, or the endpoint was unavailable - see ``_fetch_payload``'s best-effort handling of it).
        show_demographics: See :func:`_may_see_nearby_research` - False returns ``[]`` unconditionally, without even reading ``demographics``.

    Returns:
        Display rows for population, median household income, median home value, median rent, and the owner/renter split - omitting any field the ACS estimate doesn't carry."""
    if not show_demographics or not isinstance(demographics, dict):
        return []

    rows: list[dict[str, str]] = []
    if (population := _decimal_number(demographics.get("population"))) is not None:
        rows.append({"label": "Neighborhood population", "value": f"{population:,.0f}"})
    if (income := _decimal_number(demographics.get("median_household_income"))) is not None:
        rows.append({"label": "Median household income", "value": f"${income:,.0f}"})
    if (home_value := _decimal_number(demographics.get("median_home_value"))) is not None:
        rows.append({"label": "Median home value", "value": f"${home_value:,.0f}"})
    if (rent := _decimal_number(demographics.get("median_gross_rent"))) is not None:
        rows.append({"label": "Median gross rent", "value": f"${rent:,.0f}/mo"})
    owner_pct = _decimal_number(demographics.get("percent_owner_occupied"))
    renter_pct = _decimal_number(demographics.get("percent_renter_occupied"))
    if owner_pct is not None and renter_pct is not None:
        rows.append({"label": "Owner/renter occupied", "value": f"{owner_pct:.0f}% / {renter_pct:.0f}%"})
    return rows


#: Former owners shown on the card, most recently on record first.
_MAX_FORMER_OWNER_ROWS = 5


def _observed_span(owner: dict[str, Any]) -> str:
    """The years an owner was on record for the parcel, e.g. ``"1990-2005"``, or ``""`` when unknown."""
    first, last = str(owner.get("first_observed") or "")[:4], str(owner.get("last_observed") or "")[:4]
    if first and last and first != last:
        return f"{first}-{last}"
    return last or first


def _owner_rows(data: dict[str, Any]) -> list[dict[str, str]]:
    """The owner's contact details, its other parcels and the parcel's former owners, for an entitled viewer.

    Args:
        data: The cached property-record payload.

    Returns:
        Meta rows; the record's own mailing address and care-of line, else those REData holds for a current owner.
    """
    owners = [owner for owner in data.get("owners") or [] if isinstance(owner, dict) and owner.get("name")]
    current = [owner for owner in owners if owner.get("current") is not False]
    former = sorted((owner for owner in owners if owner.get("current") is False), key=lambda owner: str(owner.get("last_observed") or ""), reverse=True)

    def first_known(record_key: str, owner_key: str) -> str:
        return str(data.get(record_key) or "").strip() or next((str(owner[owner_key]) for owner in current if owner.get(owner_key)), "")

    rows: list[dict[str, str]] = []
    for label, record_key, owner_key in (("Owner mailing address", "owner_mailing_address", "mailing_address"), ("Care of", "owner_care_of", "care_of")):
        if value := first_known(record_key, owner_key):
            rows.append({"label": label, "value": value})
    for label, owner_key in (("Owner phone", "phone"), ("Owner email", "email")):
        rows.extend({"label": label, "value": str(owner[owner_key])} for owner in current if owner.get(owner_key))
    for owner in current:
        if count := owner.get("other_parcels") or 0:
            whose = f" ({owner['name']})" if len(current) > 1 else ""
            rows.append({"label": "Owner's other parcels", "value": f"{count} other parcel{'s' if count != 1 else ''} on record{whose}"})
    for owner in former[:_MAX_FORMER_OWNER_ROWS]:
        span = _observed_span(owner)
        rows.append({"label": "Former owner", "value": f"{owner['name']} (on record {span})" if span else str(owner["name"])})
    return rows


def _render_available(data: dict[str, Any], *, show_owner: bool, show_demographics: bool) -> dict[str, Any]:
    """Build the info-panel context for a successful record.

    Args:
        data: The cached property-record payload.
        show_owner: Whether this viewer may see who owns the parcel and how to reach them.
        show_demographics: Whether this viewer may see the neighbourhood demographics section - see :func:`_may_see_nearby_research` and the module docstring."""
    meta = _owner_rows(data) if show_owner else []
    if data.get("situs_address"):
        meta.append({"label": "Address", "value": data["situs_address"]})
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
        if not (total := _decimal_number(row.get("total_value"))):
            continue
        # An assessed value is a statutory fraction of market value; the
        # stage matters because a Board of Review figure is post-appeal.
        stage_suffix = f" ({row['value_stage']})" if row.get("value_stage") else ""
        meta.append({"label": f"Assessed {row.get('tax_year') or '?'}", "value": f"${total:,.0f}{stage_suffix}"})

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

    # Recorded-document references (deeds, plats).
    # Capped because a long-subdivided parcel can carry dozens.
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
    # A real point-in-boundary check (see RedataGateway.lookup_national_parks), not the
    # nearest-by-coordinate answer plugins.builtin.nps shows elsewhere on this same pin - same
    # rationale as the Special Land Use chips above: it changes what a visit is, not just describes
    # the property.
    containing_park = data.get("containing_park") or {}
    if full_name := containing_park.get("full_name"):
        chips.append(f"Situated within {full_name}")
    if data.get("field_mismatches"):
        chips.append("Sources disagree")
    if any(entry.get("delinquent") for entry in data.get("tax_history") or []):
        chips.append("Delinquent taxes")
    if data.get("parcel_geometry"):
        chips.append("Boundary available")

    # footer_link = {"url": data["source"]["url"], "label": f"View on {data['source']['provider']}"} if...

    owner_names = data.get("owner_name") or []
    if owner_names and not show_owner:
        # Named rather than silently dropped: "this parcel has a recorded
        # owner you can't see" is a different (and honest) statement from
        # "no owner on record", and the second would read as missing data.
        chips.append(OWNER_WITHHELD)

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


class PropertyRecordsPanelSource(RedataBackedSource, CoordinateGatedInfoPanelSource):
    """County property ownership/tax record card on the Private Pin page."""

    key = "property_records"
    cache_source = _CACHE_SOURCE
    #: A building pin stands on its site's parcel.
    site_level: ClassVar[bool] = True
    section_id = "parcel-records-section"
    icon = "description"
    title = "Property Records"
    placement: ClassVar[PanelPlacement] = PanelPlacement.PROPERTY
    tab_label: ClassVar[str] = "Parcel"
    tab_order: ClassVar[int] = 0
    # Deliberately not exposed on the external API: this is ownership/tax record data pulled from
    # county GIS/tax sources, and redistributing it through a bearer-key API is a different (and
    # more sensitive) exposure than showing it to a logged-in user on their own pin page.
    # Opt back in only after that's been explicitly reviewed.
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

    def site_answer_covers(self, pin: Pin, data: dict) -> bool:
        """Whether ``pin`` stands on the site's parcel; a pin across the road is on another one.

        Args:
            pin: The nested pin.
            data: The site's property-record payload.

        Returns:
            True only when the payload's parcel geometry contains the pin.
        """
        from django.contrib.gis.geos import GEOSException, GEOSGeometry, Point

        geometry = data.get("parcel_geometry")
        if not geometry:
            return False
        try:
            parcel = GEOSGeometry(json.dumps(geometry), srid=4326)
        except (GEOSException, TypeError, ValueError):
            return False
        return bool(parcel.contains(Point(float(pin.effective_longitude or 0), float(pin.effective_latitude or 0), srid=4326)))

    def adopted(self, pin: Pin, data: dict) -> None:
        """Record the site parcel's official owners and sales against the building's location too.

        Args:
            pin: The building pin.
            data: The site's property-record payload.
        """
        if data.get("available"):
            _write_official_owners_and_sales(pin.location, data)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Render the found record, the manual-lookup pointer card, or nothing (204).
        The owner's name and the demographics section are each shown only to a viewer entitled to them - see ``services.property.owner_access.viewer_of`` for who that is, and why an unresolvable viewer withholds both rather than showing them."""
        from urbanlens.dashboard.services.property.owner_access import can_see_official_owners, viewer_of

        if not data:
            return None
        if data.get("available"):
            viewer = viewer_of(pin)
            return _render_available(data, show_owner=can_see_official_owners(viewer), show_demographics=_may_see_nearby_research(viewer))
        if data.get("reason") in (REASON_MANUAL_ONLY, REASON_BLOCKED):
            return _render_manual_only(data)
        return None

    def overview_summary(self, pin: Pin, data: dict) -> OverviewSummary | None:
        """The owner, parcel number and year built, for the Property Records Overview.

        The owner's name is shown only to a viewer entitled to it, as on the Parcel tab; anyone else is told one is on
        record.
        """
        from urbanlens.dashboard.services.property.owner_access import can_see_official_owners, viewer_of

        if not data.get("available"):
            message = str(data.get("message") or "").strip().rstrip(".")
            return OverviewSummary(notes=[message]) if message and data.get("reason") in (REASON_MANUAL_ONLY, REASON_BLOCKED) else None
        chips: list[str] = []
        fields: list[dict[str, str]] = []
        if owners := [str(name) for name in data.get("owner_name") or [] if name]:
            if can_see_official_owners(viewer_of(pin)):
                fields.append({"label": "Owner", "value": ", ".join(owners)})
            else:
                chips.append(OWNER_WITHHELD)
        if data.get("apn"):
            fields.append({"label": "Parcel", "value": str(data["apn"])})
        if data.get("year_built"):
            fields.append({"label": "Year built", "value": str(data["year_built"])})
        return OverviewSummary(chips=chips, fields=fields) if chips or fields else None

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
        Without it the cycle picks candidates, every fetch raises, and the run logs one exception per location."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured

        return redata_configured()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Call REData and, on success, upsert OFFICIAL owner/sale rows.

        Args:
            location: The location to fetch a property record for.

        Returns:
            Tuple of (payload, coordinate query key) - the base class persists ``payload`` to the shared ``LocationCache`` row.

        Raises:
            PropertyRecordsUnavailableError: For a transient source outage - the enrichment runner logs it and retries the location on a later cycle instead of marking it done.
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
        """Rate-limit defaults for REData's own external API."""
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
