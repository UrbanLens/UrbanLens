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
            "field_sources": dict(self.field_sources),
            "field_mismatches": list(self.field_mismatches),
            "tax_history": [entry.to_dict() for entry in self.tax_history],
            "sales_history": [entry.to_dict() for entry in self.sales_history],
            "deed_document_links": list(self.deed_document_links),
            "source": self.source.to_dict(),
            "confidence": self.confidence,
        }
