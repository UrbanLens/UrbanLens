"""How a place's name and address read, as pure functions of their components.
Keeping the rules here rather than reimplementing them against the projection is what stops the two from drifting - a fallback chain changed on the model but not in the payload is invisible until a user reports the wrong name on their map."""

from __future__ import annotations

#: Country spellings treated as the USA when choosing an area format.
USA_COUNTRY_NAMES = frozenset({"united states", "united states of america", "usa", "us"})


def is_usa(country: str | None) -> bool:
    """Whether *country* identifies the USA.

    Args:
        country: The location's country component, possibly blank or None.

    Returns:
        True when the country is blank or a recognized USA spelling."""
    normalized = (country or "").replace(".", "").strip().casefold()
    return not normalized or normalized in USA_COUNTRY_NAMES


def area_label(*, city: str | None, state: str | None, country: str | None) -> str | None:
    """A short human-readable area, e.g. ``Albany, NY`` or ``Kyiv, Ukraine``.

    Args:
        city: The location's city component.
        state: The location's state component.
        country: The location's country component.

    Returns:
        The area string, or None when no components are available."""
    city = (city or "").strip()
    state = (state or "").strip()
    country_text = (country or "").strip()
    parts = [city, state] if is_usa(country) else [city or state, country_text]
    return ", ".join(part for part in parts if part) or None


def display_name(*, wiki_name: str | None, official_name: str | None, city: str | None, state: str | None, country: str | None) -> str:
    """The best human-readable name for a place.

    Args:
        wiki_name: The linked wiki's name, if the place has a wiki.
        official_name: The place's externally supplied name.
        city: The location's city component.
        state: The location's state component.
        country: The location's country component.

    Returns:
        The name to display."""
    if wiki_name:
        return wiki_name
    if official_name:
        return official_name
    if area := area_label(city=city, state=state, country=country):
        return f"Unnamed Location in {area}"
    return "Unnamed Location"


def street_address(*, street_number: str | None, route: str | None) -> str | None:
    """The street number and route, joined, or None when neither is known.

    Args:
        street_number: The location's street-number component.
        route: The location's street-name component.

    Returns:
        e.g. ``"123 Main St"``, or None.
    """
    return " ".join(part for part in (street_number, route) if part) or None


def formatted_address(*, address_basic: str | None, city: str | None, state: str | None) -> str | None:
    """A "street, city, state" address, omitting the components that are absent.

    Args:
        address_basic: The street address.
        city: The city component.
        state: The state component.

    Returns:
        The joined address, or None when there is no street address."""
    if not address_basic:
        return None
    parts = [address_basic]
    if city:
        parts.append(city)
    if state:
        parts.append(state)
    return ", ".join(parts)
