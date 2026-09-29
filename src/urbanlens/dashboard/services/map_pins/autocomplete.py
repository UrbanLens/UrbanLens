"""Autocomplete search service for the map address search bar."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.services.core.request_upstream import UpstreamResult

logger = logging.getLogger(__name__)


@dataclass
class AutocompleteResult:
    """A single autocomplete suggestion returned to the client."""

    type: str  # pin | location | place | address | coordinates
    title: str
    subtitle: str
    lat: float | None
    lng: float | None
    zoom: int
    icon: str  # Material Icons ligature name
    pin_slug: str | None = None
    place_id: str | None = None  # Google place_id for deferred coordinate resolution
    is_child: bool = False  # True for child (sub) pins nested under a parent pin

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "type": self.type,
            "title": self.title,
            "subtitle": self.subtitle,
            "lat": self.lat,
            "lng": self.lng,
            "zoom": self.zoom,
            "icon": self.icon,
            "pin_slug": self.pin_slug,
            "place_id": self.place_id,
            "is_child": self.is_child,
        }


def search_local(query: str, profile) -> list[AutocompleteResult]:
    """Search the local DB for pins, locations, and their aliases matching *query*.

    Args:
        query: Raw search string (may be a partial word).
        profile: The requesting user's Profile instance.

    Returns:
        Up to 12 ordered AutocompleteResult items, most relevant first."""
    from django.db.models import Q

    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.locations.external_tag_groups import tag_match_q
    from urbanlens.dashboard.services.wiki.concealment import concealment_active
    from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_locations_cached

    results: list[AutocompleteResult] = []
    q = query.strip()
    if len(q) < 2:
        return results

    q_lower = q.lower()
    seen_pin_ids: set[int] = set()

    # -- Pin search --------------------------------------------------------------- Single query
    # with OR across all relevant text fields.
    # Deliberately not restricted to root pins: jumping to a child (sub) pin must always work, even
    # though the map hides child pins unless their layer is on - the map turns the layer on when the
    # Match-then-fetch: the predicate runs against ids alone, then those rows are fetched by
    # primary key. Carrying `select_related` through the matching query costs ~210ms of planning
    # per keystroke at capacity scale - ten relations and two hundred columns to de-duplicate (X28).
    matching_ids = (
        Pin.objects.filter(profile=profile)
        .filter(
            Q(name__icontains=q)
            | Q(aliases__name__icontains=q)
            | Q(description__icontains=q)
            | Q(labels__name__icontains=q)
            | Q(location__official_name__icontains=q)
            | Q(location__wiki__name__icontains=q)
            | Q(location__wiki__aliases__name__icontains=q)
            | Q(location__wiki__description__icontains=q)
            | tag_match_q(q, "location__place__external_tags"),
        )
        .match_ids(12)
    )
    from urbanlens.dashboard.services.global_search.providers import ConcealedWikiText, _terms_survive

    concealed_text = ConcealedWikiText(profile)
    pin_qs = Pin.objects.by_ids(matching_ids).select_related("location__wiki", "parent_pin", "parent_pin__location").prefetch_related("labels", "aliases", "location__wiki__aliases").order_by("id")

    for pin in pin_qs:
        if pin.id in seen_pin_ids:
            continue
        seen_pin_ids.add(pin.id)

        # A pin's own fields justify surfacing it regardless of its wiki's concealment state -
        # nothing wiki-scoped is being disclosed.
        # Wiki.objects.get_for_location, not the reverse accessor directly - `Location.wiki` raises
        # RelatedObjectDoesNotExist rather than returning None when the location has no wiki yet,
        wiki = Wiki.objects.get_for_location(pin.location) if pin.location_id and pin.location is not None else None
        if wiki is not None and concealment_active(wiki, profile) and not _pin_own_fields_match(pin, q_lower) and not _terms_survive([q_lower], concealed_text.haystacks(wiki)):
            continue

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            continue

        is_child = pin.parent_pin_id is not None
        if is_child and pin.parent_pin is not None:
            subtitle = f"Child pin of {pin.parent_pin.effective_name or 'a pin'}"
        else:
            subtitle = _pin_match_subtitle(pin, q_lower, profile)
        results.append(
            AutocompleteResult(
                type="pin",
                title=pin.effective_name or "Unnamed",
                subtitle=subtitle,
                lat=float(lat),
                lng=float(lng),
                zoom=17 if is_child else 16,
                icon="subdirectory_arrow_right" if is_child else "push_pin",
                pin_slug=pin.slug or str(pin.uuid),
                is_child=is_child,
            ),
        )

    seen_wiki_ids: set[int] = set()
    # Match-then-fetch again, for the reason the pin half above does it. The order is the
    # primary key's because the single-statement form it replaces had none of its own, so which
    # five a match of more than five returned was whatever the plan happened to emit.
    wiki_ids = (
        Wiki.objects.filter(location_id__in=visible_wiki_locations_cached(profile))
        .filter(
            Q(name__icontains=q) | Q(aliases__name__icontains=q) | Q(description__icontains=q) | tag_match_q(q, "location__place__external_tags"),
        )
        .match_ids(5)
    )
    wikis = list(Wiki.objects.by_ids(wiki_ids).select_related("location").order_by("id"))
    concealed_text.load(wiki for wiki in wikis if concealment_active(wiki, profile))

    for wiki in wikis:
        if wiki.id in seen_wiki_ids:
            continue
        seen_wiki_ids.add(wiki.id)
        if wiki.location is None or wiki.location.latitude is None or wiki.location.longitude is None:
            continue
        # Re-verify against what this viewer would actually be shown - the SQL match above ran
        # against the live name/description/aliases, which is precisely what concealment exists to
        # hide.
        # Surviving that check only says the wiki may appear in the list; the title itself must
        from urbanlens.dashboard.services.wiki.concealment import conceal_wiki

        if concealment_active(wiki, profile) and not _terms_survive([q_lower], concealed_text.haystacks(wiki)):
            continue
        results.append(
            AutocompleteResult(
                type="location",
                title=conceal_wiki(wiki, profile).name,
                subtitle="Community wiki",
                lat=float(wiki.location.latitude),
                lng=float(wiki.location.longitude),
                zoom=16,
                icon="public",
            ),
        )

    return results


def _pin_own_fields_match(pin, q_lower: str) -> bool:
    """Whether *q_lower* matches the pin's own data, ignoring anything wiki-scoped."""
    if q_lower in (pin.name or "").lower():
        return True
    if any(q_lower in alias.name.lower() for alias in pin.aliases.all()):
        return True
    if q_lower in (pin.description or "").lower():
        return True
    if any(q_lower in label.name.lower() for label in pin.labels.all()):
        return True
    if pin.location is not None and q_lower in (pin.location.official_name or "").lower():
        return True
    return _place_has_matching_tag(pin.location.place if pin.location is not None else None, q_lower)


def _place_has_matching_tag(place, term: str) -> bool:
    """Whether *place* itself carries a tag equivalent to *term* (see :func:`matching_vocabulary`)."""
    if place is None:
        return False
    from urbanlens.dashboard.services.locations.external_tag_groups import matching_vocabulary

    entries = matching_vocabulary(term)
    if not entries:
        return False
    matching_tuples = {(entry.source, entry.key, entry.value) for entry in entries}
    return any((tag.source, tag.key, tag.value) in matching_tuples for tag in place.external_tags.all())


def _pin_match_subtitle(pin, q_lower: str, profile) -> str:
    """Return a one-line subtitle that explains why *pin* matched *q_lower*."""
    from urbanlens.dashboard.services.wiki.concealment import conceal_rows, conceal_wiki, concealment_active

    pin_name = (pin.name or "").lower()

    # Direct name match - use location as context
    if q_lower in pin_name:
        return pin.location.display_name if pin.location else "Your pin"

    # Alias match
    for alias in pin.aliases.all():
        if q_lower in alias.name.lower():
            return f'Also known as "{alias.name}"'

    # Description / notes match - show a short excerpt
    if pin.description and q_lower in pin.description.lower():
        desc = pin.description
        idx = desc.lower().find(q_lower)
        start = max(0, idx - 20)
        snippet = desc[start : idx + 40].strip()
        if start > 0:
            snippet = "..." + snippet
        if idx + 40 < len(desc):
            snippet += "..."
        return snippet

    # Label / tag match
    for label in pin.labels.all():
        if q_lower in label.name.lower():
            return f"Tagged: {label.name}"

    # Wiki/display name and wiki-alias matches both need the concealed value
    # when this pin's wiki is concealed for the viewer - Location.display_name
    # prefers the live wiki name, which is exactly what concealment hides.
    wiki = pin.wiki
    conceal = wiki is not None and concealment_active(wiki, profile)
    display_name = conceal_wiki(wiki, profile).name if conceal else (pin.location.display_name if pin.location else None)

    if display_name and q_lower in display_name.lower():
        return display_name

    if wiki is not None:
        aliases = conceal_rows(wiki.aliases.all(), profile) if conceal else wiki.aliases.all()
        for alias in aliases:
            if q_lower in alias.name.lower():
                return f'Wiki alias: "{alias.name}"'

    return display_name or "Your pin"


#: Place predictions change far more slowly than people retype the same prefix.
PLACES_AUTOCOMPLETE_TTL = 86400
#: A place's coordinates; Google's terms allow caching a place's latitude and longitude for 30 days.
PLACE_RESOLVE_TTL = 86400


def _normalised_query(query: str) -> str:
    return " ".join(query.lower().split())


def search_google_places(query: str, api_key: str, *, caller: str | None = None) -> UpstreamResult[list[AutocompleteResult]]:
    """Place predictions for the search bar, from cache or REData/Google (the API key stays server-side).
    Coordinates are omitted here; `resolve_google_place` fetches them only for the suggestion the user picks.

    Args:
        query: User's search text.
        api_key: Google Maps / Places API key, used only when REData is not configured.
        caller: Who to charge against the per-account rate, or None when the route is throttled elsewhere.

    Returns:
        Up to 6 suggestions without coordinates, or why there are none.
    """
    from urbanlens.dashboard.services.apis.locations import places_resolution
    from urbanlens.dashboard.services.apis.request_upstreams import PlacesAutocompleteUpstream

    def fetch() -> list[AutocompleteResult]:
        results: list[AutocompleteResult] = []
        for pred in places_resolution.autocomplete_predictions(query, api_key=api_key)[:6]:
            title = pred.get("main_text") or ""
            place_id = pred.get("place_id")
            if not place_id or not title:
                continue
            results.append(AutocompleteResult(type="place", title=title, subtitle=pred.get("secondary_text") or "", lat=None, lng=None, zoom=15, icon="place", place_id=place_id))
        return results

    key = f"{places_resolution.active_provider()}:{_normalised_query(query)}"
    return PlacesAutocompleteUpstream.call(fetch, key=key, ttl=PLACES_AUTOCOMPLETE_TTL, caller=caller)


#: OpenStreetMap places change slowly, and the same towns and streets are looked up by everyone.
NOMINATIM_SEARCH_TTL = 86400
NOMINATIM_DEFAULT_LIMIT = 5
NOMINATIM_MAX_LIMIT = 10

type Viewbox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class GeocodeResult:
    """One OpenStreetMap place, reduced to what the search bar shows and flies to."""

    lat: float
    lon: float
    name: str
    display_name: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {"lat": self.lat, "lon": self.lon, "name": self.name, "display_name": self.display_name}


def parse_viewbox(raw: str | None) -> Viewbox | None:
    """Read a ``west,south,east,north`` bias box from a query string.

    Args:
        raw: The comma-separated value, or None.

    Returns:
        The four coordinates, or None when *raw* is blank.

    Raises:
        ValueError: When *raw* is not exactly four finite numbers.
    """
    if raw is None or not raw.strip():
        return None
    west, south, east, north = (float(part) for part in raw.split(","))
    box = (west, south, east, north)
    if not all(math.isfinite(value) for value in box):
        raise ValueError("viewbox coordinates must be finite")
    return box


def clamp_nominatim_limit(raw: str | None) -> int:
    """The number of results to ask for, from a client-supplied value.

    Args:
        raw: The ``limit`` query parameter, or None.

    Returns:
        *raw* bounded to 1..:data:`NOMINATIM_MAX_LIMIT`, or :data:`NOMINATIM_DEFAULT_LIMIT` when it is not an integer.
    """
    try:
        limit = int(raw) if raw is not None else NOMINATIM_DEFAULT_LIMIT
    except ValueError:
        limit = NOMINATIM_DEFAULT_LIMIT
    return max(1, min(limit, NOMINATIM_MAX_LIMIT))


def search_nominatim(query: str, *, limit: int, viewbox: Viewbox | None = None, caller: str | None = None) -> UpstreamResult[list[GeocodeResult]]:
    """OpenStreetMap places matching *query*, from cache or Nominatim.

    Args:
        query: User's search text.
        limit: Most results to return.
        viewbox: Box to prefer results inside, without excluding those outside it.
        caller: Who to charge against the per-account rate, or None when the route is throttled elsewhere.

    Returns:
        Places with coordinates, or why there are none.
    """
    from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
    from urbanlens.dashboard.services.apis.request_upstreams import NominatimSearchUpstream

    params: dict[str, Any] = {"accept-language": "en"}
    viewbox_param = ""
    if viewbox is not None:
        viewbox_param = ",".join(f"{value:.6f}" for value in viewbox)
        params |= {"viewbox": viewbox_param, "bounded": 0}

    def fetch() -> list[GeocodeResult]:
        results: list[GeocodeResult] = []
        for place in NominatimGateway().search(query, limit=limit, **params):
            try:
                lat, lon = float(place["lat"]), float(place["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            results.append(GeocodeResult(lat=lat, lon=lon, name=place.get("name") or "", display_name=place.get("display_name") or ""))
        return results

    key = f"{_normalised_query(query)}|{limit}|{viewbox_param}"
    # The gateway answers a failed request with an empty list, so only a non-empty answer is known good.
    return NominatimSearchUpstream.call(fetch, key=key, ttl=NOMINATIM_SEARCH_TTL, caller=caller, cacheable=bool)


def empty_suggestions(profile) -> list[AutocompleteResult]:
    """Return suggestions for an empty search input: top cities by pin count.

    Args:
        profile: The requesting user's Profile instance.

    Returns:
        Up to 2 city suggestions ordered by descending pin count."""
    from django.db.models import Count

    from urbanlens.dashboard.models.pin import Pin

    results: list[AutocompleteResult] = []

    city_rows = (
        Pin.objects.filter(profile=profile)
        .root_pins()
        .filter(location__isnull=False)
        .filter(location__locality__isnull=False)
        .exclude(location__locality="")
        .values(
            "location__locality",
            "location__administrative_area_level_1",
        )
        .annotate(pin_count=Count("id"))
        .order_by("-pin_count")[:2]
    )

    for row in city_rows:
        locality = row["location__locality"]
        state = row["location__administrative_area_level_1"] or ""
        count = row["pin_count"]

        rep_pin = Pin.objects.filter(profile=profile, location__locality=locality).root_pins().select_related("location").first()
        if rep_pin is None:
            continue
        lat = rep_pin.effective_latitude
        lng = rep_pin.effective_longitude
        if lat is None or lng is None:
            continue

        city_label = f"{locality}, {state}" if state else locality
        results.append(
            AutocompleteResult(
                type="city",
                title=city_label,
                subtitle=f"{count} pin{'s' if count != 1 else ''}",
                lat=float(lat),
                lng=float(lng),
                zoom=12,
                icon="location_city",
            ),
        )

    return results


def resolve_google_place(place_id: str, api_key: str, *, caller: str | None = None) -> UpstreamResult[tuple[float | None, float | None, str | None]]:
    """Coordinates for a place_id the user selected, from cache or REData/Google.

    Args:
        place_id: Places ``place_id`` from an autocomplete prediction.
        api_key: Google Maps / Places API key, used only when REData is not configured.
        caller: Who to charge against the per-account rate, or None when the route is throttled elsewhere.

    Returns:
        ``(latitude, longitude, name)`` - either coordinate None when the place has none - or why there is no answer.
    """
    from urbanlens.dashboard.services.apis.locations import places_resolution
    from urbanlens.dashboard.services.apis.request_upstreams import PlaceResolveUpstream

    key = f"{places_resolution.active_provider()}:{place_id}"
    return PlaceResolveUpstream.call(lambda: places_resolution.resolve_place_coordinates(place_id, api_key=api_key), key=key, ttl=PLACE_RESOLVE_TTL, caller=caller)
