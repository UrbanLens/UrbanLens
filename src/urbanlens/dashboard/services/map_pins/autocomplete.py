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

    Covers:
    - Pin name (effective name)
    - Pin aliases (PinAlias)
    - Pin personal notes / description
    - Label / tag names assigned to the pin
    - Location canonical name
    - Wiki aliases (WikiAlias / community wiki aliases)
    - Location description

    Args:
        query: Raw search string (may be a partial word).
        profile: The requesting user's Profile instance.

    Returns:
        Up to 12 ordered AutocompleteResult items, most relevant first.
    """
    from django.db.models import Q

    from urbanlens.dashboard.models.pin import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.locations.external_tag_groups import tag_match_q
    from urbanlens.dashboard.services.wiki.concealment import concealment_active
    from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids_cached

    results: list[AutocompleteResult] = []
    q = query.strip()
    if len(q) < 2:
        return results

    q_lower = q.lower()
    seen_pin_ids: set[int] = set()

    # -- Pin search ---------------------------------------------------------------
    # Single query with OR across all relevant text fields. Deliberately not
    # restricted to root pins: jumping to a child (sub) pin must always work,
    # even though the map hides child pins unless their layer is on - the map
    # turns the layer on when the user jumps to one (see _onLocationSelect).
    pin_qs = (
        Pin.objects.filter(profile=profile)
        .select_related("location__wiki", "parent_pin", "parent_pin__location")
        .prefetch_related("labels", "aliases", "location__wiki__aliases")
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
        .distinct()[:12]
    )

    for pin in pin_qs:
        if pin.id in seen_pin_ids:
            continue
        seen_pin_ids.add(pin.id)

        # A pin's own fields justify surfacing it regardless of its wiki's
        # concealment state - nothing wiki-scoped is being disclosed. But the
        # query above also matches via location__wiki__* clauses, and a pin
        # whose *only* reason for matching is a concealed wiki's live
        # name/description/alias is exactly the substring oracle
        # docs/PROBLEMS.md's 2026-08-24 entry describes, one hop further out.
        # Wiki.objects.get_for_location, not the reverse accessor directly -
        # `Location.wiki` raises RelatedObjectDoesNotExist rather than
        # returning None when the location has no wiki yet, even with
        # select_related (the descriptor caches "there is none" as an
        # exception, not a cached None).
        wiki = Wiki.objects.get_for_location(pin.location) if pin.location_id and pin.location is not None else None
        if wiki is not None and concealment_active(wiki, profile) and not _pin_own_fields_match(pin, q_lower):
            from urbanlens.dashboard.services.global_search.providers import _concealed_wiki_haystacks, _terms_survive

            if not _terms_survive([q_lower], _concealed_wiki_haystacks(wiki, profile)):
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

    # -- Wiki search (community pages) -------------------------------------------
    # Scoped to the wikis this user can actually open, asked of the access
    # authority rather than restated here: "has a pin on the exact location" is
    # one of its four clauses, so a pin sharing the place's domain opened the
    # wiki page while its name refused to autocomplete.
    seen_wiki_ids: set[int] = set()
    wiki_qs = (
        Wiki.objects.filter(
            Q(name__icontains=q) | Q(aliases__name__icontains=q) | Q(description__icontains=q) | tag_match_q(q, "location__place__external_tags"),
        )
        .filter(location_id__in=visible_wiki_location_ids_cached(profile))
        .select_related("location")
        .distinct()[:5]
    )

    for wiki in wiki_qs:
        if wiki.id in seen_wiki_ids:
            continue
        seen_wiki_ids.add(wiki.id)
        if wiki.location is None or wiki.location.latitude is None or wiki.location.longitude is None:
            continue
        # Re-verify against what this viewer would actually be shown - the SQL
        # match above ran against the live name/description/aliases, which is
        # precisely what concealment exists to hide. Surviving that check only
        # says the wiki may appear in the list; the title itself must still
        # come from the concealed value, or a term matched via a friend's
        # alias would display the wiki's true, stranger-renamed title.
        from urbanlens.dashboard.services.wiki.concealment import conceal_wiki

        if concealment_active(wiki, profile):
            from urbanlens.dashboard.services.global_search.providers import _concealed_wiki_haystacks, _terms_survive

            if not _terms_survive([q_lower], _concealed_wiki_haystacks(wiki, profile)):
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
    """Whether *q_lower* matches the pin's own data, ignoring anything wiki-scoped.

    Used to tell "this pin matched on its own name/notes/labels" (always fine
    to surface, concealment or not) apart from "this pin matched only via its
    location's wiki" (needs the concealed-content re-check). A place's
    external tags count as "own data" the same way ``location.official_name``
    already does below - they're provider (OSM/Overture) facts about the
    place, not wiki-authored content.
    """
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
    from urbanlens.dashboard.services.apis.locations import places_resolution

    results: list[AutocompleteResult] = []
    try:
        predictions = places_resolution.autocomplete_predictions(query, api_key=api_key)
        for pred in predictions[:6]:
            title = pred.get("main_text") or ""
            subtitle = pred.get("secondary_text") or ""
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


def resolve_google_place(
    place_id: str,
    api_key: str,
) -> tuple[float | None, float | None, str | None]:
    """Look up coordinates for a Google place_id selected by the user.

    Args:
        place_id: Google Places place_id from an autocomplete prediction.
        api_key: Google Maps / Places API key.

    Returns:
        (latitude, longitude, name) - all may be None on failure.
    """
    from urbanlens.dashboard.services.apis.locations import places_resolution

    try:
        return places_resolution.resolve_place_coordinates(place_id, api_key=api_key)
    except Exception:
        logger.warning("Google place resolution failed for %s", place_id, exc_info=True)

    return None, None, None
