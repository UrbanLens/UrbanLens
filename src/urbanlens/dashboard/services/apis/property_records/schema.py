"""The standardized property-record schema every source tier normalizes into.

Mirrors the target shape in ``docs/property-records-plan.md`` section 0.
Every retrieval tier builds one ``PropertyRecord`` regardless of which
upstream service answered, so the pin-detail panel and the enrichment writer
never need to know which tier or provider produced the data they're
rendering.

``source``/``confidence`` describe the record as a whole - for the common
case where only one tier answered for a jurisdiction, that's the complete
picture. When more than one tier contributed (``merge.merge_records``, per
the plan's section 4), ``field_sources`` records which tier's data won for
each individual field, and ``field_mismatches`` flags fields where tiers
disagreed outright rather than one simply being blank - both are empty for a
single-tier record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any


def _utc_now() -> datetime:
    """Timezone-aware UTC now - records travel through JSON caches and must never be naive local time."""
    return datetime.now(UTC)


def _date_iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True, slots=True)
class AssessedValue:
    """A single tax year's assessed value breakdown.

    ``year`` is optional (``None`` rather than a guessed year) - many county
    GIS layers expose current land/improvement/total values without ever
    labeling which tax year they're for.
    """

    year: int | None = None
    land: float | None = None
    improvement: float | None = None
    total: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"year": self.year, "land": self.land, "improvement": self.improvement, "total": self.total}


@dataclass(frozen=True, slots=True)
class TaxHistoryEntry:
    """One tax year's billing/payment status."""

    year: int
    amount: float | None = None
    paid: bool | None = None
    paid_date: date | None = None
    delinquent: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"year": self.year, "amount": self.amount, "paid": self.paid, "paid_date": _date_iso(self.paid_date), "delinquent": self.delinquent}


@dataclass(frozen=True, slots=True)
class SaleHistoryEntry:
    """One recorded sale/transfer of the parcel."""

    sale_date: date | None = None
    price: float | None = None
    grantor: str = ""
    grantee: str = ""
    doc_type: str = ""
    doc_number: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": _date_iso(self.sale_date),
            "price": self.price,
            "grantor": self.grantor,
            "grantee": self.grantee,
            "doc_type": self.doc_type,
            "doc_number": self.doc_number,
        }


@dataclass(frozen=True, slots=True)
class BuildingCharacteristics:
    """Physical-building details, when a source's Tier 1 layer reports them.

    Deliberately a separate nested object rather than flat ``PropertyRecord``
    fields - mirrors ``AssessedValue``'s grouping, and keeps these
    building-specific fields out of ``relevance.PARCEL_FIELD_CANDIDATES``'s
    "does this look like comprehensive parcel data" signal (see
    ``field_mapping._SUPPLEMENTARY_CANDIDATES``'s docstring for why: a real
    discovery false positive had exactly these fields on a narrow,
    non-comprehensive subset).
    """

    stories: float | None = None
    roof_material: str | None = None
    wall_material: str | None = None
    garage: str | None = None
    heating_type: str | None = None
    quality: str | None = None
    condition: str | None = None
    building_count: int | None = None
    outbuilding_value: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stories": self.stories,
            "roof_material": self.roof_material,
            "wall_material": self.wall_material,
            "garage": self.garage,
            "heating_type": self.heating_type,
            "quality": self.quality,
            "condition": self.condition,
            "building_count": self.building_count,
            "outbuilding_value": self.outbuilding_value,
        }


@dataclass(frozen=True, slots=True)
class RecordSource:
    """Provenance for a ``PropertyRecord`` - which tier/provider answered, and when."""

    tier: int
    provider: str
    url: str = ""
    retrieved_at: datetime = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "provider": self.provider, "url": self.url, "retrieved_at": self.retrieved_at.isoformat()}


@dataclass(frozen=True, slots=True)
class PropertyRecord:
    """Standardized ownership/tax/sale record for one parcel, from any tier.

    Attributes mirror ``docs/property-records-plan.md`` section 0's target
    schema. Every field beyond ``situs_address`` and ``source`` is optional -
    a real county source rarely populates all of them, and a sparse but
    genuinely-sourced record is still useful (e.g. owner name with no tax
    history at all).
    """

    situs_address: str
    county: str
    state: str
    fips: str
    source: RecordSource
    confidence: float
    parcel_id: str = ""
    apn: str = ""
    owner_name: tuple[str, ...] = ()
    owner_mailing_address: str | None = None
    legal_description: str | None = None
    land_use_code: str | None = None
    lot_size_sqft: float | None = None
    building_sqft: float | None = None
    year_built: int | None = None
    assessed_value: AssessedValue | None = None
    market_value: float | None = None
    tax_history: tuple[TaxHistoryEntry, ...] = ()
    sales_history: tuple[SaleHistoryEntry, ...] = ()
    deed_document_links: tuple[str, ...] = ()
    #: Legal zoning district (e.g. "R-1") - distinct from ``land_use_code``
    #: (the assessor's current-use classification); a parcel's zoning and its
    #: actual current use routinely differ.
    zoning_code: str | None = None
    tax_district: str | None = None
    school_district: str | None = None
    #: Exemption/abatement category (homestead, agricultural, veteran, ...),
    #: when the source reports one - not whether taxes are current (see
    #: ``tax_history``'s ``delinquent`` flag for that).
    exemption_type: str | None = None
    #: Present-use-value / agricultural-deferral amount - the gap between a
    #: parcel's market value and its (lower) taxed value under a
    #: conservation/agricultural program, when the source reports one.
    deferred_value: float | None = None
    subdivision_name: str | None = None
    neighborhood: str | None = None
    #: Physical-building details, when the source reports any (see
    #: :class:`BuildingCharacteristics`).
    building_characteristics: BuildingCharacteristics | None = None
    #: Earlier parcel/PIN identifiers this record was renumbered from -
    #: county GIS systems that migrate platforms often carry a "previous ID"
    #: field so historical references still resolve.
    prior_parcel_ids: tuple[str, ...] = ()
    #: The parcel's own boundary, when the source is a Tier 1 ArcGIS layer
    #: queried with ``returnGeometry=true``/``outSR=4326`` (Socrata sources
    #: and Tier 2/3 scrapes never populate this). Deliberately stored as
    #: Esri's own ring-list shape - ``{"format": "esri_rings",
    #: "spatial_reference": "EPSG:4326", "rings": [[[lon, lat], ...], ...]}`` -
    #: rather than converted to strict GeoJSON: correctly regrouping Esri's
    #: flat ring list into GeoJSON's nested exterior/hole Polygon structure
    #: needs real per-ring signed-area winding-order logic this schema has no
    #: other reason to carry, and the stored shape is already trivial for a
    #: future Leaflet-based consumer to draw directly.
    parcel_geometry: dict[str, Any] | None = None
    #: Field name -> tier number that supplied it. Only non-empty when
    #: ``merge.merge_records`` combined more than one tier's results.
    field_sources: dict[str, int] = field(default_factory=dict)
    #: Field names where multiple tiers returned different non-empty values -
    #: the plan's "flag mismatches rather than silently picking one."
    field_mismatches: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict, suitable for a ``LocationCache`` row."""
        return {
            "parcel_id": self.parcel_id,
            "apn": self.apn,
            "situs_address": self.situs_address,
            "county": self.county,
            "state": self.state,
            "fips": self.fips,
            "owner_name": list(self.owner_name),
            "owner_mailing_address": self.owner_mailing_address,
            "legal_description": self.legal_description,
            "land_use_code": self.land_use_code,
            "lot_size_sqft": self.lot_size_sqft,
            "building_sqft": self.building_sqft,
            "year_built": self.year_built,
            "assessed_value": self.assessed_value.to_dict() if self.assessed_value else None,
            "market_value": self.market_value,
            "zoning_code": self.zoning_code,
            "tax_district": self.tax_district,
            "school_district": self.school_district,
            "exemption_type": self.exemption_type,
            "deferred_value": self.deferred_value,
            "subdivision_name": self.subdivision_name,
            "neighborhood": self.neighborhood,
            "building_characteristics": self.building_characteristics.to_dict() if self.building_characteristics else None,
            "prior_parcel_ids": list(self.prior_parcel_ids),
            "parcel_geometry": self.parcel_geometry,
            "field_sources": dict(self.field_sources),
            "field_mismatches": list(self.field_mismatches),
            "tax_history": [entry.to_dict() for entry in self.tax_history],
            "sales_history": [entry.to_dict() for entry in self.sales_history],
            "deed_document_links": list(self.deed_document_links),
            "source": self.source.to_dict(),
            "confidence": self.confidence,
        }
