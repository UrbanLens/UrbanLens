"""
Autocomplete search service for the map address search bar.

Searches the local database for pins, locations, and their aliases; and can
proxy Google Places Autocomplete requests to hide the API key from the browser.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AutocompleteResult:
    """A single autocomplete suggestion returned to the client."""

    type: str           # pin | location | place | address | coordinates
    title: str
    subtitle: str
    lat: float | None
    lng: float | None
    zoom: int
    icon: str           # Material Icons ligature name
    pin_slug: str | None = None
    place_id: str | None = None  # Google place_id for deferred coordinate resolution

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
        }


def search_local(query: str, profile) -> list[AutocompleteResult]:
    """Search the local DB for pins, locations, and their aliases matching *query*.

    Covers:
    - Pin name (effective name)
    - Pin aliases (PinAlias)
    - Pin personal notes / description
    - Badge / tag names assigned to the pin
    - Location canonical name
    - Location aliases (LocationAlias / community wiki aliases)
    - Location description

    Args:
        query: Raw search string (may be a partial word).
        profile: The requesting user's Profile instance.

    Returns:
        Up to 12 ordered AutocompleteResult items, most relevant first.
    """
    from django.db.models import Q

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin import Pin

    results: list[AutocompleteResult] = []
    q = query.strip()
    if len(q) < 2:
        return results

    q_lower = q.lower()
    seen_pin_ids: set[int] = set()

    # ── Pin search ───────────────────────────────────────────────────────────────
    # Single query with OR across all relevant text fields.
    pin_qs = (
        Pin.objects.filter(profile=profile)
        .root_pins()
        .select_related("location")
        .prefetch_related("badges", "aliases", "location__aliases")
        .filter(
            Q(name__icontains=q)
            | Q(aliases__name__icontains=q)
            | Q(description__icontains=q)
            | Q(badges__name__icontains=q)
            | Q(location__name__icontains=q)
            | Q(location__aliases__name__icontains=q)
            | Q(location__description__icontains=q),
        )
        .distinct()[:12]
    )

    for pin in pin_qs:
        if pin.id in seen_pin_ids:
            continue
        seen_pin_ids.add(pin.id)

        lat = pin.effective_latitude
        lng = pin.effective_longitude
        if lat is None or lng is None:
            continue

        subtitle = _pin_match_subtitle(pin, q_lower)
        results.append(
            AutocompleteResult(
                type="pin",
                title=pin.effective_name or "Unnamed",
                subtitle=subtitle,
                lat=float(lat),
                lng=float(lng),
                zoom=16,
                icon="push_pin",
                pin_slug=pin.slug or str(pin.uuid),
            ),
        )

    # ── Location search (community wiki) ────────────────────────────────────────
    # Only show locations the requesting user has actually pinned so results stay relevant.
    seen_loc_ids: set[int] = set()
    loc_qs = (
        Location.objects.filter(
            Q(name__icontains=q)
            | Q(aliases__name__icontains=q)
            | Q(description__icontains=q),
        )
        .filter(pins__profile=profile)
        .distinct()[:5]
    )

    for loc in loc_qs:
        if loc.id in seen_loc_ids:
            continue
        seen_loc_ids.add(loc.id)
        if loc.latitude is None or loc.longitude is None:
            continue
        results.append(
            AutocompleteResult(
                type="location",
                title=loc.name,
                subtitle="Community wiki",
                lat=float(loc.latitude),
                lng=float(loc.longitude),
                zoom=16,
                icon="public",
            ),
        )

    return results


def _pin_match_subtitle(pin, q_lower: str) -> str:
    """Return a one-line subtitle that explains why *pin* matched *q_lower*."""
    pin_name = (pin.name or "").lower()
    loc_name = (pin.location.name if pin.location else "").lower()

    # Direct name match — use location as context
    if q_lower in pin_name:
        return pin.location.name if pin.location else "Your pin"

    # Alias match
    for alias in pin.aliases.all():
        if q_lower in alias.name.lower():
            return f'Also known as "{alias.name}"'

    # Description / notes match — show a short excerpt
    if pin.description and q_lower in pin.description.lower():
        desc = pin.description
        idx = desc.lower().find(q_lower)
        start = max(0, idx - 20)
        snippet = desc[start : idx + 40].strip()
        if start > 0:
            snippet = "…" + snippet
        if idx + 40 < len(desc):
            snippet += "…"
        return snippet

    # Badge / tag match
    for badge in pin.badges.all():
        if q_lower in badge.name.lower():
            return f"Tagged: {badge.name}"

    # Location name match
    if q_lower in loc_name:
        return pin.location.name

    # Location alias match
    if pin.location:
        for alias in pin.location.aliases.all():
            if q_lower in alias.name.lower():
                return f'Location alias: "{alias.name}"'

    return pin.location.name if pin.location else "Your pin"


def search_google_places(query: str, api_key: str) -> list[AutocompleteResult]:
    """Proxy a Google Places Autocomplete request (hides the API key from the browser).

    Coordinates are intentionally omitted here; they are resolved lazily in
    `resolve_google_place` only when the user selects a suggestion.

    Args:
        query: User's search text.
        api_key: Google Maps / Places API key.

    Returns:
        Up to 6 place suggestions without coordinates.
    """
    from urbanlens.dashboard.services.google.places import GooglePlacesGateway

    results: list[AutocompleteResult] = []
    try:
        gw = GooglePlacesGateway(api_key=api_key)
        predictions = gw.autocomplete(query)
        for pred in predictions[:6]:
            fmt = pred.get("structured_formatting", {})
            title = fmt.get("main_text") or pred.get("description", "")
            subtitle = fmt.get("secondary_text") or ""
            place_id = pred.get("place_id")
            if not place_id or not title:
                continue
            results.append(
                AutocompleteResult(
                    type="place",
                    title=title,
                    subtitle=subtitle,
                    lat=None,
                    lng=None,
                    zoom=15,
                    icon="place",
                    place_id=place_id,
                ),
            )
    except Exception:
        logger.warning("Google Places autocomplete failed", exc_info=True)

    return results


def empty_suggestions(profile) -> list[AutocompleteResult]:
    """Return suggestions for an empty search input: top cities by pin count.

    Used when the search bar is focused but empty, giving the user quick
    navigation shortcuts based on where they have the most pins.

    Args:
        profile: The requesting user's Profile instance.

    Returns:
        Up to 2 city suggestions ordered by descending pin count.
    """
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

        rep_pin = (
            Pin.objects.filter(profile=profile, location__locality=locality)
            .root_pins()
            .select_related("location")
            .first()
        )
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


def resolve_google_place(
    place_id: str, api_key: str,
) -> tuple[float | None, float | None, str | None]:
    """Look up coordinates for a Google place_id selected by the user.

    Args:
        place_id: Google Places place_id from an autocomplete prediction.
        api_key: Google Maps / Places API key.

    Returns:
        (latitude, longitude, name) — all may be None on failure.
    """
    from urbanlens.dashboard.services.google.places import GooglePlacesGateway

    try:
        gw = GooglePlacesGateway(api_key=api_key)
        details = gw.get_place_details(place_id, fields=["geometry", "name"])
        loc = details.get("geometry", {}).get("location", {})
        lat = loc.get("lat")
        lng = loc.get("lng")
        name = details.get("name")
        if lat is not None and lng is not None:
            return float(lat), float(lng), name
    except Exception:
        logger.warning("Google place resolution failed for %s", place_id, exc_info=True)

    return None, None, None
