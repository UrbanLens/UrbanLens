"""Historical features plugin: mapped buildings/roads/etc. that once stood near a pin, via REData.

Retrospectively-traced built environment - mostly demolished, mostly never
formally designated - covering what stood on or near a site before it looked
the way it does today. Distinct from Historic Registers
(``redata_historic_registers``, a body's own designation) and USGS Historical
Topo Maps (a scanned page): this is per-feature data with its own validity
interval, most useful for "what was here before" rather than "is this
protected".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Show at most this many features in the panel's meta grid.
_MAX_ROWS = 8


class HistoricalFeaturesPanelSource(RedataInfoPanelSource):
    """Mapped historical features within 250 m of the pin, standing or long gone."""

    key = "redata_historical_features"
    cache_source = "redata_historical_features"
    section_id = "historical-features-section"
    icon = "history_edu"
    title = "Historical Features"

    payload_key: ClassVar[str] = "features"

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Historical buildings/roads/water/etc. mapped near the pin."""
        from urbanlens.dashboard.services.apis.locations.redata_historical_features_gateway import RedataHistoricalFeaturesGateway

        return RedataHistoricalFeaturesGateway().get_historical_features(latitude, longitude, limit=25)

    def transform_rows(self, rows: list[dict]) -> list[dict]:
        """Drop each feature's geometry before caching.

        The panel renders names/kinds/years only; a Polygon/LineString per
        feature would bloat the cache row for nothing.
        """
        return [{key: value for key, value in feature.items() if key != "geometry"} for feature in rows]

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """List the most recently-attested features first, undated ones excluded from the count claim."""
        from urbanlens.dashboard.services.apis.locations.redata_historical_features_gateway import HISTORICAL_FEATURE_KIND_LABELS

        features = (data or {}).get("features") or []
        if not features:
            return None

        # A null end_year means "not known to have ended", never "still
        # standing" - see RedataHistoricalFeaturesGateway's module docstring
        # - so this chip names what the data actually shows: no recorded end.
        undated_end = [feature for feature in features if feature.get("start_year") is not None and feature.get("end_year") is None]
        chips = [f"{len(features)} traced within 250 m"]
        if undated_end:
            chips.append(f"{len(undated_end)} with no recorded end date")

        meta = []
        for feature in features[:_MAX_ROWS]:
            kind = feature.get("kind") or "other"
            label = HISTORICAL_FEATURE_KIND_LABELS.get(kind, "Feature")
            name = feature.get("name") or label
            start = feature.get("start_year")
            end = feature.get("end_year")
            # `is not None` rather than truthiness: a year of 0 is a valid
            # (if unlikely) value and must not be silently dropped like an
            # absent bound would be.
            if start is not None and end is not None:
                span = f"{start}–{end}"
            elif start is not None:
                span = f"documented from {start}"
            elif end is not None:
                span = f"documented until {end}"
            else:
                span = ""
            note = feature.get("source_note") or ""
            value = ", ".join(part for part in (span, note) if part)
            meta.append({"label": name, "value": value or label})

        return {"chips": chips, "meta": meta}


class HistoricalFeaturesPlugin(UrbanLensPlugin):
    """Retrospectively-traced historical features near pinned locations, sourced through REData."""

    name: ClassVar[str] = "redata_historical_features"
    verbose_name: ClassVar[str] = "Historical Features"
    description: ClassVar[str] = "Shows buildings, roads, water features, railways and other historical features mapped near the pin, sourced through REData's OpenHistoricalMap registry."
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the historical-features pin-detail panel."""
        return [HistoricalFeaturesPanelSource()]
