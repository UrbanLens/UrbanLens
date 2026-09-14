"""Police incidents plugin: block-scale incident reports near a pin, via REData."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.pins.external_data import PanelSource

_MAX_ROWS = 6
_YEARS = 3

# default is 3, the free panel's window above) - see
# ``RedataIncidentsGateway.get_incidents``.
_HISTORY_YEARS = 25
_HISTORY_LIMIT = 500
_HISTORY_MAX_ROWS = 15

_TOP_CATEGORIES = 3

# The publishers fuzz coordinates to block scale before release; every panel
# built from these rows says so rather than letting a point imply
# address-level knowledge.
_PRECISION_CAVEAT: dict[str, str] = {"label": "Precision", "value": "Locations are approximate (block scale, as published)"}


def _top_category_chips(incidents: list[dict], category_labels: dict[str, str]) -> list[str]:
    """Tally incidents by category and format the most common as chip strings.

    Args:
        incidents: Incident rows, each with a ``category`` key.
        category_labels: Maps a raw category key to its display label.

    Returns:
        Up to :data:`_TOP_CATEGORIES` chip strings like ``"5x Burglary"``, most frequent category first."""
    by_category: dict[str, int] = {}
    for incident in incidents:
        category = incident.get("category") or "other"
        by_category[category] = by_category.get(category, 0) + 1
    top = sorted(by_category.items(), key=lambda item: -item[1])[:_TOP_CATEGORIES]
    return [f"{count}x {category_labels.get(category, category)}" for category, count in top]


class PoliceIncidentsPanelSource(RedataInfoPanelSource):
    """Reported police incidents within the pin's block (500 m, publisher-pinned)."""

    key = "redata_incidents"
    cache_source = "redata_incidents"
    section_id = "police-incidents-section"
    icon = "local_police"
    title = "Reported Incidents"
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    payload_key: ClassVar[str] = "incidents"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Block-scale police incident reports near the pin."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import RedataIncidentsGateway

        return RedataIncidentsGateway().get_incidents(latitude, longitude, years=_YEARS, limit=50)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Summarize by category, then list the most recent incidents."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import INCIDENT_CATEGORY_LABELS

        incidents = (data or {}).get("incidents") or []
        # Traffic collisions are police *reports* rather than crime, and only
        # some feeds publish any - including them would make the same block
        # look "worse" in one city than another purely by publishing scope.
        incidents = [incident for incident in incidents if incident.get("category") != "traffic"]
        if not incidents:
            return None

        chips = [f"{len(incidents)} on this block in {_YEARS} years"]
        chips.extend(_top_category_chips(incidents, INCIDENT_CATEGORY_LABELS))

        meta = []
        for incident in incidents[:_MAX_ROWS]:
            occurred = (incident.get("occurred_at") or "")[:10]
            category = INCIDENT_CATEGORY_LABELS.get(incident.get("category") or "other", "Other")
            description = (incident.get("offense_description") or "").strip().capitalize()
            meta.append({"label": occurred or "Undated", "value": f"{category}" + (f" - {description}" if description else "")})

        meta.append(_PRECISION_CAVEAT)

        return {"chips": chips, "meta": meta}


class IncidentHistoryPanelSource(RedataInfoPanelSource):
    """Full REData incident history (up to 25 years) for the pin's block, gated to subscribers.
    A deeper research pull than :class:`PoliceIncidentsPanelSource`'s free safety-context snapshot: instead of the most recent handful of reports over 3 years, this shows the year-by-year trend across REData's whole retention window."""

    key = "redata_incident_history"
    cache_source = "redata_incident_history"
    section_id = "incident-history-section"
    icon = "history"
    title = "Incident History"
    geo_boundary: ClassVar[GeoBoundary | None] = USA
    required_feature: ClassVar[SiteFeature | None] = SiteFeature.INCIDENT_HISTORY

    payload_key: ClassVar[str] = "incidents"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """The full 25-year incident window near the pin."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import RedataIncidentsGateway

        # force_refresh=True: REData's coverage cache has no `years` dimension - it keys purely on
        # coordinate + a radius pinned the same for every provider - so a cached hit here could
        # silently be the free panel's narrower 3-year fetch.
        return RedataIncidentsGateway().get_incidents(latitude, longitude, years=_HISTORY_YEARS, limit=_HISTORY_LIMIT, force_refresh=True)

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Summarize the whole window by year, most recent first."""
        from urbanlens.dashboard.services.apis.locations.redata_incidents_gateway import INCIDENT_CATEGORY_LABELS

        incidents = (data or {}).get("incidents") or []
        # Same exclusion as the free panel, for the same reason: traffic
        # collisions are police reports, not crime, and comparing counts
        # across cities would compare publishing scope rather than safety.
        incidents = [incident for incident in incidents if incident.get("category") != "traffic"]
        if not incidents:
            return None

        by_year: dict[str, int] = {}
        for incident in incidents:
            occurred = incident.get("occurred_at") or ""
            year = occurred[:4] if len(occurred) >= 4 else "Undated"
            by_year[year] = by_year.get(year, 0) + 1

        chips = [f"{len(incidents)} on this block in {_HISTORY_YEARS} years"]
        chips.extend(_top_category_chips(incidents, INCIDENT_CATEGORY_LABELS))

        # Numeric years first, most recent leading; "Undated" trails rather
        # than sorting ahead of every real year (a plain string sort would
        # put it first - "U" > "2").
        ordered_years = sorted((year for year in by_year if year != "Undated"), reverse=True)
        ordered_years.extend(year for year in by_year if year == "Undated")

        meta = [{"label": year, "value": f"{by_year[year]} incident{'s' if by_year[year] != 1 else ''}"} for year in ordered_years[:_HISTORY_MAX_ROWS]]
        meta.append(_PRECISION_CAVEAT)

        return {"chips": chips, "meta": meta}


class PoliceIncidentsPlugin(UrbanLensPlugin):
    """Block-scale reported-incident context for pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_incidents"
    verbose_name: ClassVar[str] = "Reported Police Incidents"
    description: ClassVar[str] = "Shows recent reported police incidents on the pin's block as visit-safety context, from city open-data portals via REData. Locations are block-scale by publication; traffic collisions are excluded."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the reported-incidents panel and its gated incident-history sibling."""
        return [PoliceIncidentsPanelSource(), IncidentHistoryPanelSource()]
