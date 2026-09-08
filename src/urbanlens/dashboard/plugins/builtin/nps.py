"""National Park Service plugin: nearby-park panel on the Private Pin page.

Backed by REData's local NPS catalog (``services.apis.locations.redata_national_parks_gateway``),
a pure proximity search rather than the boundary-containment lookup this
project used before (the direct NPS Developer API + an ArcGIS point-in-polygon
query, now removed - REData has no raw-coordinate containment endpoint of its
own, only one keyed by a REData parcel uuid this project doesn't otherwise
resolve for most pins). The panel and enrichment source below therefore show
the nearest NPS unit within REData's search radius, not strictly a park the
pin is inside - a real precision tradeoff of this migration, worth knowing if
a pin near a park's edge shows that park despite technically sitting just
outside its boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import redata_configured
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.dashboard.services.geo.geo_boundary import USA
from urbanlens.dashboard.services.locations.enrichment import LocationCacheEnrichmentSource
from urbanlens.dashboard.services.locations.name_resolution import LocationCacheNameProvider
from urbanlens.dashboard.services.pins.external_data import LocationCachePanelSource, PanelApiKind, info_card

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway
    from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.pins.external_data import PanelSource

#: How many of a park's activity tags become chips. NPS lists dozens for a big
#: unit ("Hiking", "Wildlife Watching", "Astronomy", ...); past the first
#: handful they stop characterizing the place and just become a wall of pills.
#: Mirrors the ``|slice:":8"`` the HTML panel applies for the same reason.
_MAX_ACTIVITY_CHIPS = 8

#: How many active alerts become quick-facts. NPS alert lists are ordinarily
#: short, but nothing upstream bounds them, and this is the one section of the
#: card that must never silently balloon past its budget.
_MAX_ALERT_FACTS = 8

#: How many facility names to spell out in the "Visitor Centers"/"Campgrounds"
#: meta rows before collapsing the rest into "+N more" - same reasoning as
#: :data:`_MAX_ACTIVITY_CHIPS`: past a handful, names stop being useful and
#: just take up card space.
_MAX_FACILITY_NAMES = 5


#: NPS's ``standardHours`` keys, in the order a week is read. Lowercase because
#: that is what its API emits; the display labels are the values.
_WEEK: tuple[tuple[str, str], ...] = (
    ("monday", "Mon"),
    ("tuesday", "Tue"),
    ("wednesday", "Wed"),
    ("thursday", "Thu"),
    ("friday", "Fri"),
    ("saturday", "Sat"),
    ("sunday", "Sun"),
)


def entrance_fee_summary(fees: Any) -> str:
    """One line describing what it costs to get in.

    "Is it free" is the question this answers, and for the NPS catalog it is a
    real one: most units charge nothing and a minority charge per vehicle. The
    panel previously cached ``entrance_fees`` and showed none of it.

    Args:
        fees: REData's ``entrance_fees`` - NPS's own ``entranceFees`` list of
            ``{"cost": "35.00", "title": ..., "description": ...}``. ``cost``
            is a *string* in NPS's API, including for free entry ("0.00").

    Returns:
        A display string, or ``""`` when the list is absent or unusable.
        Absent is not free: a unit whose fees NPS has not published must not be
        advertised as costing nothing.
    """
    if not isinstance(fees, list):
        return ""

    priced: list[tuple[float, str]] = []
    for fee in fees:
        if not isinstance(fee, dict):
            continue
        raw_cost = str(fee.get("cost") or "").strip()
        if not raw_cost:
            continue
        try:
            cost = float(raw_cost)
        except ValueError:
            # NPS publishes free text here for some units ("varies", "See
            # below"). Skipping is the honest answer; guessing a number is not.
            continue
        title = str(fee.get("title") or "").strip()
        # NPS prefixes almost every title with "Entrance Fee - "; the prefix is
        # the column header, not part of the answer.
        for prefix in ("Entrance Fee - ", "Entrance Fee-", "Entrance Fee "):
            if title.startswith(prefix):
                title = title[len(prefix) :].strip()
                break
        priced.append((cost, title))

    if not priced:
        return ""
    if all(cost == 0 for cost, _ in priced):
        return "Free"

    cheapest, title = min(priced, key=lambda item: item[0])
    label = f"${cheapest:,.2f}"
    if title:
        label = f"{label} ({title})"
    return label if len(priced) == 1 else f"From {label}"


def standard_hours_summary(operating_hours: Any) -> str:
    """When the place is open, collapsed into day ranges.

    The template used to render "Standard hours vary - check NPS.gov" whenever
    ``standardHours`` was present, which is the one case where it did *not*
    have to say that: the hours were cached and readable.

    Consecutive days with identical hours are grouped, so the common shapes
    read as "Open daily" or "Mon-Fri: 9:00AM - 5:00PM; Sat-Sun: Closed" rather
    than as seven lines.

    Args:
        operating_hours: REData's ``operating_hours`` - NPS's own list of
            ``{"name": ..., "standardHours": {"monday": ..., ...}}``. The first
            entry is the park itself; later ones are individual visitor centres
            and are not what a pin-detail summary is about.

    Returns:
        A display string, or ``""`` when no usable hours are published.
    """
    if not isinstance(operating_hours, list) or not operating_hours:
        return ""
    first = operating_hours[0]
    hours = first.get("standardHours") if isinstance(first, dict) else None
    if not isinstance(hours, dict):
        return ""

    days = [(label, str(hours.get(key) or "").strip()) for key, label in _WEEK]
    if any(not value for _, value in days):
        # A partially-published week cannot be collapsed honestly - saying
        # "Mon-Wed: 9-5" while Thursday is simply unknown reads as "closed
        # Thursday", which is a different claim.
        return ""

    groups: list[tuple[str, str, str]] = []
    for label, value in days:
        if groups and groups[-1][2] == value:
            groups[-1] = (groups[-1][0], label, value)
        else:
            groups.append((label, label, value))

    if len(groups) == 1:
        return f"{groups[0][2]} daily"
    return "; ".join(f"{start}: {value}" if start == end else f"{start}-{end}: {value}" for start, end, value in groups)


def alert_facts(data: dict[str, Any]) -> list[dict[str, str]]:
    """Published alerts as icon-led quick facts - the safety-critical section of the card.

    NPS's own alert categories run from routine ("Information") to urgent
    ("Park Closure", "Danger"); REData does not rank them and this project has
    no authority to invent a severity order, so alerts are shown in whatever
    order REData returns them rather than resorted here.

    Args:
        data: The cached park payload (``alerts`` key -
            :meth:`RedataNationalParksGateway.get_alerts`-shaped dicts).

    Returns:
        ``{"icon", "text", "href"?}`` rows, one per alert, capped at
        :data:`_MAX_ALERT_FACTS` - ``[]`` when nothing is published.
    """
    alerts = data.get("alerts")
    if not isinstance(alerts, list):
        return []

    facts: list[dict[str, str]] = []
    for alert in alerts[:_MAX_ALERT_FACTS]:
        if not isinstance(alert, dict):
            continue
        title = str(alert.get("title") or "").strip()
        if not title:
            continue
        category = str(alert.get("category") or "").strip()
        fact: dict[str, str] = {"icon": "warning", "text": f"{category}: {title}" if category else title}
        if url := str(alert.get("url") or "").strip():
            fact["href"] = url
        facts.append(fact)
    return facts


def _facility_summary(rows: Any) -> str:
    """Render a facility list (visitor centers, campgrounds) as ``"<n> (<name1>, <name2>, ...)"``.

    A reasonable minimal treatment for facets that can run long: the count is
    always exact even when the name list is truncated, so "12 (...)" never
    understates how many there are.

    Args:
        rows: REData's raw facility rows, each carrying at least ``name``.

    Returns:
        A display string, or ``""`` when there are no rows.
    """
    if not isinstance(rows, list) or not rows:
        return ""
    names = [name for row in rows if isinstance(row, dict) and (name := str(row.get("name") or "").strip())]
    shown = ", ".join(names[:_MAX_FACILITY_NAMES])
    remainder = len(names) - _MAX_FACILITY_NAMES
    if remainder > 0:
        shown = f"{shown}, +{remainder} more" if shown else f"+{remainder} more"
    return f"{len(rows)} ({shown})" if shown else str(len(rows))


def park_facts(data: dict[str, Any]) -> list[dict[str, str]]:
    """The park facts worth showing beside its name, as ``{label, value, href}`` rows.

    Shared by the web panel and :meth:`NpsPanelSource.api_payload` so the two
    cannot drift - the web template previously rendered a subset by hand and
    the API a different subset.

    Reads fields REData has been caching and nothing was displaying: entrance
    fees, published hours, and the park's own directions page, plus the
    visitor-center/campground facets cached alongside the park itself (see
    :meth:`NpsPanelSource.fetch`). ``weather_info`` is deliberately left out -
    it is a paragraph of seasonal prose, and this pin already has a weather
    panel showing the actual forecast. Alerts are deliberately *not* here -
    they are safety-critical and go through :func:`alert_facts` into the
    card's icon-led ``facts`` instead, so they render ahead of this whole
    section rather than mixed into a label/value grid.

    Args:
        data: The cached park payload.

    Returns:
        Display rows, omitting anything the park does not publish.
    """
    rows: list[dict[str, str]] = []
    for key, label in (("designation", "Designation"), ("states", "States")):
        if data.get(key):
            rows.append({"label": label, "value": str(data[key])})
    if fee := entrance_fee_summary(data.get("entrance_fees")):
        rows.append({"label": "Entry", "value": fee})
    if hours := standard_hours_summary(data.get("operating_hours")):
        rows.append({"label": "Hours", "value": hours})
    if visitor_centers := _facility_summary(data.get("visitor_centers")):
        rows.append({"label": "Visitor Centers", "value": visitor_centers})
    if campgrounds := _facility_summary(data.get("campgrounds")):
        rows.append({"label": "Campgrounds", "value": campgrounds})
    if directions := str(data.get("directions_url") or "").strip():
        rows.append({"label": "Directions", "value": "Getting there", "href": directions})
    # Last: it is a cross-reference ("HUTR"), not something a reader wants
    # before the opening hours.
    if data.get("park_code"):
        rows.append({"label": "Park Code", "value": str(data["park_code"])})
    return rows


def _park_with_facets(gateway: RedataNationalParksGateway, park: dict[str, Any] | None) -> dict[str, Any]:
    """Attach a park's alerts/visitor-centers/campgrounds to its nearby-search row.

    Shared by :meth:`NpsPanelSource.fetch` and :meth:`NpsEnrichmentSource.fetch`
    so the two cache the same shape. These three facets are per-park-unit, not
    per-coordinate, so they are only worth fetching once a nearest park has
    actually been found - nothing within range means no ``park_code`` to fetch
    them by.

    Args:
        gateway: The gateway to fetch the facets through.
        park: The nearest-park dict from :meth:`RedataNationalParksGateway.find_nearest_park`,
            or None when nothing was within range.

    Returns:
        A copy of ``park`` (``{}`` when None) with ``alerts``,
        ``visitor_centers`` and ``campgrounds`` keys added.
    """
    data = dict(park) if park else {}
    if park_code := data.get("park_code"):
        data["alerts"] = gateway.get_alerts(park_code)
        data["visitor_centers"] = gateway.get_visitor_centers(park_code)
        data["campgrounds"] = gateway.get_campgrounds(park_code)
    return data


class NpsPanelSource(LocationCachePanelSource):
    """National Park Service information for the pin's location."""

    key = "nps"
    cache_source = "nps"
    section_id = "nps-section"
    icon = "park"
    title = "National Park Service"
    # Bespoke markup on the web (a hero photo, prose, activity chips), but the
    # facts underneath are an ordinary information card, so the API serves it
    # through the same INFO contract every other panel uses rather than
    # inventing an NPS-shaped response only this one plugin's clients know.
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO})

    def gate(self, pin: Pin) -> bool:
        """Requires REData to be configured."""
        return redata_configured()

    def fetch(self, pin: Pin) -> None:
        """Cache the nearest NPS park unit to the pin, if any is within REData's search radius.

        Once a unit is found, also fetches and caches its alerts, visitor
        centers and campgrounds - all three are per-park-unit facets, so
        there is nothing to fetch them by until a park has actually been
        resolved.
        """
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

        location = pin.location
        lat = float(pin.effective_latitude or 0)
        lng = float(pin.effective_longitude or 0)
        gateway = RedataNationalParksGateway()
        park = gateway.find_nearest_park(lat, lng)
        query_key = f"{lat:.5f},{lng:.5f}"
        LocationCache.set(location, self.cache_source, _park_with_facets(gateway, park), query_key=query_key)

    def api_payload(self, pin: Pin) -> dict[str, Any] | None:
        """The nearest NPS unit as an information card, or None.

        Mirrors ``PinController.nps_info``'s own emptiness rule: a cached
        payload with no ``full_name`` means the fetch ran and found no park
        unit within range - a settled "nothing here" rather than a pending
        state - the web panel 204s on it and the API omits it.

        Args:
            pin: The pin whose panel is being read.

        Returns:
            ``{"info": {...}}``, or None when nothing has landed yet or no
            park unit was found within range.
        """
        data = self.cached_data(pin)
        if not data or not data.get("full_name"):
            return None

        park_url = data.get("url") or ""
        images = data.get("images") or []
        first_image = images[0] if isinstance(images, list) and images else {}

        return {
            PanelApiKind.INFO.value: info_card(
                heading_name=data.get("full_name"),
                chips=[activity.get("name") for activity in (data.get("activities") or [])[:_MAX_ACTIVITY_CHIPS] if isinstance(activity, dict)],
                # Alerts go in `facts`, not `meta`: every info-card consumer
                # (see `_simple_info_panel.html`, the shape's other renderer)
                # reads `facts` ahead of `meta`, so a closure or hazard
                # reaches a client before routine facts like hours - never
                # buried behind them.
                facts=alert_facts(data),
                meta=park_facts(data),
                header_link={"url": park_url, "label": "View on NPS.gov"} if park_url else None,
                footer_link={"url": park_url, "label": "View on NPS.gov"} if park_url else None,
                image_url=first_image.get("url") if isinstance(first_image, dict) else None,
                description=data.get("description"),
            ),
        }


class NpsEnrichmentSource(LocationCacheEnrichmentSource):
    """Background-fills the nearest-national-park cache (a name/alias source) per Location."""

    key: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    cache_source: ClassVar[str] = "nps"
    service_keys: ClassVar[tuple[str, ...]] = ("redata_national_parks",)
    geo_boundary: ClassVar[GeoBoundary | None] = USA

    def gate(self) -> bool:
        """Requires REData to be configured."""
        return redata_configured()

    def fetch(self, location: Location) -> tuple[dict | None, str]:
        """Look up the nearest NPS unit to a location, if any is within range.

        Once a unit is found, also fetches its alerts, visitor centers and
        campgrounds - see :meth:`NpsPanelSource.fetch`.

        Args:
            location: The location to check.

        Returns:
            Tuple of (park payload or None, coordinate query key).
        """
        from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

        lat = float(location.latitude or 0)
        lng = float(location.longitude or 0)
        gateway = RedataNationalParksGateway()
        park = gateway.find_nearest_park(lat, lng)
        query_key = f"{lat:.5f},{lng:.5f}"
        if park is None:
            return None, query_key
        return _park_with_facets(gateway, park), query_key


class NpsPlugin(UrbanLensPlugin):
    """National Park Service information for pinned locations, via REData."""

    name: ClassVar[str] = "nps"
    verbose_name: ClassVar[str] = "National Park Service"
    description: ClassVar[str] = "Shows nearby US national park information on the Private Pin page, via REData's local NPS catalog. USA only."
    author: ClassVar[str] = "UrbanLens"

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for REData's national-park catalog lookup."""
        return {
            "redata_national_parks": ServiceDefaults(
                display_name="REData (national park catalog)",
                calls_per_minute=120,
                calls_per_day=10000,
                usa_only=True,
                notes="Our own standalone REData service, not a third-party budget - just a sanity ceiling.",
            ),
        }

    def get_panel_sources(self) -> list[PanelSource]:
        """Contribute the NPS pin-detail panel."""
        return [NpsPanelSource()]

    def get_name_providers(self) -> list[NameProvider]:
        """Contribute the nearest park's name as a place-name candidate."""
        return [LocationCacheNameProvider(source="nps", cache_source="nps", keys=("full_name",), verbose_name="National Park Service")]

    def get_enrichment_sources(self) -> list[EnrichmentSource]:
        """Contribute the nearest-park cache to scheduled background enrichment."""
        return [NpsEnrichmentSource()]
