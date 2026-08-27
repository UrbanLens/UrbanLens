"""Building permits plugin: permit/violation history for a pin's site, via REData.

A site's filing chronology - what work was permitted, what was cited, and
(where the city publishes it) a deep link to the filing's own record and plan
drawings. Providers are cities (Chicago, New York, San Francisco, Austin,
Seattle), so the panel gates on the USA and quietly hides outside a covered
city.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.pins.external_data import PanelSource

_MAX_ROWS = 8
_YEARS = 10


class BuildingPermitsPanelSource(RedataInfoPanelSource):
    """Permit, violation and site-plan filings within 150 m of the pin."""

    key = "redata_permits"
    cache_source = "redata_permits"
    section_id = "building-permits-section"
    icon = "construction"
    title = "Permits & Violations"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    payload_key: ClassVar[str] = "filings"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Permit/violation filings for the pin's site."""
        from urbanlens.dashboard.services.apis.locations.redata_permits_gateway import RedataPermitsGateway

        return RedataPermitsGateway().get_permits(latitude, longitude, years=_YEARS, limit=25)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Build the filing chronology (already ordered by issued/cited date)."""
        from urbanlens.dashboard.services.apis.locations.redata_permits_gateway import PERMIT_KIND_LABELS

        filings = (data or {}).get("filings") or []
        if not filings:
            return None

        by_kind: dict[str, int] = {}
        for filing in filings:
            kind = filing.get("kind") or "permit"
            by_kind[kind] = by_kind.get(kind, 0) + 1
        chips = [f"{count} {PERMIT_KIND_LABELS.get(kind, kind).lower()}{'s' if count != 1 else ''}" for kind, count in sorted(by_kind.items())]
        # A dense block fills the portal's page size; the count is then a
        # floor, not a total, and presenting it unqualified would misread.
        if any((filing.get("attributes") or {}).get("result_capped") for filing in filings):
            chips.append("more than shown")

        meta = []
        for filing in filings[:_MAX_ROWS]:
            issued = (filing.get("issued_at") or "")[:10]
            kind_label = PERMIT_KIND_LABELS.get(filing.get("kind") or "", "Filing")
            parts = [filing.get("work_type") or kind_label]
            if filing.get("status"):
                parts.append(str(filing["status"]))
            cost = filing.get("estimated_cost")
            if isinstance(cost, (int, float)) and cost > 0:
                parts.append(f"declared ${cost:,.0f}")
            meta.append(
                {
                    "label": f"{kind_label} - {issued or 'undated'}",
                    "value": ", ".join(parts),
                    "href": filing.get("url") or "",
                },
            )

        return {"chips": chips, "meta": meta}


class BuildingPermitsPlugin(UrbanLensPlugin):
    """Building-permit and code-violation history for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_permits"
    verbose_name: ClassVar[str] = "Building Permits & Violations"
    description: ClassVar[str] = "Shows the pin site's permit, violation and site-plan filing history on the detail page, with deep links to city records and plan drawings where published, sourced through REData's municipal-portals registry."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the permits/violations pin-detail panel."""
        return [BuildingPermitsPanelSource()]
