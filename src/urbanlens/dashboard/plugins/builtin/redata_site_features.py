"""Site features plugin: cameras, towers and other mapped fixtures near a pin, via REData.

REData's points-of-interest registry holds about two dozen providers, and until
now UrbanLens reached exactly two of them (``yelp`` and ``epa_echo``, each behind
its own panel). The rest are the ones closest to what this app is for:
surveillance-camera registers published by individual agencies, plus
``osm_surveillance`` - contributed rather than authoritative, but worldwide, and
outside Chicago and Austin the only camera source there is - and ``fcc_asr``,
every FCC-registered antenna structure. Alongside them sit FAA facility groups,
EPA contamination programmes, underground storage tanks and school layers.

**No provider list is hardcoded here, deliberately.** Most of these providers are
generated on REData's side from dataset tables - one per camera register, one per
FAA facility group - so their tags are not knowable to a client, and a list
written here would silently stop growing the day REData added a register. The
panel asks ``GET /capabilities/?lat=&lng=`` which providers cover the point (a
bounds test, no external call on REData's side) and requests those.

What it *does* name is two small exclusion sets, kept apart because they can go
stale for different reasons. :data:`_SHOWN_ELSEWHERE` is the handful of providers
UrbanLens already gives a dedicated panel: that is a fact about this app's own UI,
so a test can hold every entry to a registered panel key, and a new REData
provider still appears here automatically. :data:`_TOO_GENERIC` is one judgement
about REData's own taxonomy, which is the only thing here a REData change could
invalidate - so it is exactly one entry, and says so.

Rows are grouped by REData's normalized ``category`` (free text, one value per
provider - "Red-light camera", "Speed camera", "Antenna structure"), so the panel
organizes itself from the data rather than from a mapping that would need
extending in step with REData.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.pins.redata_panel import RedataInfoPanelSource

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: Providers that already have a panel of their own, so including them here would
#: show the same facilities twice under a vaguer heading. Named by UrbanLens's own
#: UI rather than by anything REData publishes, which is what makes this list safe
#: to write down: each entry must match a registered panel key, and
#: ``test_redata_site_features`` fails if one stops doing so.
_SHOWN_ELSEWHERE: frozenset[str] = frozenset(
    {
        "epa_echo",  # plugins.builtin.epa_echo - its own exact-site card and nearby list
        "yelp",  # plugins.builtin.yelp
        "nps_places",  # plugins.builtin.nps
    },
)

#: Providers left out for what they *contain* rather than for where else they are
#: shown. Unlike :data:`_SHOWN_ELSEWHERE` this is a judgement about REData's own
#: taxonomy, and therefore the one thing here that can go stale - it is kept to a
#: single entry for that reason, and stated rather than folded in silently.
#:
#: ``osm`` is the generic OpenStreetMap point set: benches, waste baskets, post
#: boxes. Under a panel called "Cameras & Structures", grouped by category, those
#: rows would not be wrong so much as make the panel about nothing in particular.
#: OpenStreetMap's *camera* data reaches this panel by another route -
#: ``osm_surveillance`` is its own provider and is not excluded.
_TOO_GENERIC: frozenset[str] = frozenset({"osm"})

#: Rows shown before the list is truncated. These registries are dense in a city
#: centre - a block can hold a dozen cameras - and the panel is a summary, not an
#: inventory.
_MAX_ROWS = 10


class SiteFeaturesPanelSource(RedataInfoPanelSource):
    """Cameras, antenna structures and other mapped fixtures near the pin."""

    key = "redata_site_features"
    cache_source = "redata_site_features"
    section_id = "site-features-section"
    icon = "photo_camera_front"
    title = "Cameras & Structures"
    payload_key: ClassVar[str] = "features"
    #: A dense city block and an empty field both fetch successfully; only one
    #: has a tab worth showing.
    inspects_content: ClassVar[bool] = True

    def fetch_envelope(self, latitude: float, longitude: float) -> LocationContextEnvelope:
        """Ask every applicable provider except the ones with their own panel."""
        from urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway import RedataPointsOfInterestGateway, applicable_provider_tags

        excluded = _SHOWN_ELSEWHERE | _TOO_GENERIC
        wanted = [tag for tag in applicable_provider_tags(latitude, longitude) if tag not in excluded]
        if not wanted:
            # Nothing covers this point (or discovery failed). An empty envelope
            # rather than an unfiltered request: asking with no `provider` would
            # fan out across the whole registry, which is the one thing the
            # capability lookup exists to avoid.
            from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope as Envelope

            return Envelope(count=0, complete=True, results=[], providers=[])

        gateway = RedataPointsOfInterestGateway()
        return gateway.near_point("/api/v1/points-of-interest/lookup/", latitude, longitude, provider=wanted)

    def has_content(self, data: dict | None) -> bool:
        """A row with neither a name nor a category renders nothing worth a tab."""
        return bool(feature_rows((data or {}).get(self.payload_key) or []))

    def render_context(self, pin: Pin, data: dict) -> dict | None:
        """Group the features by REData's own category label."""
        rows = feature_rows((data or {}).get(self.payload_key) or [])
        if not rows:
            return None

        by_category: dict[str, int] = {}
        for row in rows:
            by_category[row["category"]] = by_category.get(row["category"], 0) + 1

        chips = [f"{count} {category.lower()}{'s' if count != 1 else ''}" for category, count in sorted(by_category.items(), key=lambda item: (-item[1], item[0]))]
        meta = [{"label": row["category"], "value": row["name"], "href": row["url"]} for row in rows[:_MAX_ROWS]]
        return {"chips": chips, "meta": meta}


def feature_rows(features: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalize REData point-of-interest rows into display rows.

    Reads only the promoted fields every provider in the registry answers -
    ``name``, ``category``, ``description``, ``url`` - never the per-provider
    ``attributes`` blob. That is what lets one panel render a camera register, an
    antenna structure and a storage tank without knowing anything about any of
    them.

    Args:
        features: Raw ``PointOfInterestSerializer`` rows.

    Returns:
        ``{"category", "name", "url"}`` dicts, nearest-first order preserved,
        skipping rows that would render as an unlabelled blank.
    """
    rows: list[dict[str, str]] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        category = str(feature.get("category") or "").strip()
        name = str(feature.get("name") or "").strip() or str(feature.get("description") or "").strip()
        if not category and not name:
            continue
        rows.append({"category": category or "Feature", "name": name or category, "url": str(feature.get("url") or "")})
    return rows


class SiteFeaturesPlugin(UrbanLensPlugin):
    """Mapped cameras, antenna structures and similar fixtures near a pin, via REData."""

    name: ClassVar[str] = "redata_site_features"
    verbose_name: ClassVar[str] = "Cameras & Structures"
    description: ClassVar[str] = (
        "Shows mapped surveillance cameras (agency registers plus OpenStreetMap's worldwide contributed set), "
        "FCC-registered antenna structures, and other fixtures near the pin, from REData's points-of-interest "
        "registry. Which sources are asked is discovered from REData's own capability index rather than listed "
        "here, so a register REData adds appears without an UrbanLens release."
    )
    author: ClassVar[str] = "UrbanLens"

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the site-features pin-detail panel."""
        return [SiteFeaturesPanelSource()]
