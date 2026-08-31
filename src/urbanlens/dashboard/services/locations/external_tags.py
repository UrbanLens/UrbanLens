"""Extraction of classification tags from already-fetched provider payloads.

Turns the Nominatim/Overture response shapes this app already fetches (for
their own info panels) into ``PlaceExternalTag.sync_for_source``-ready
:class:`~urbanlens.dashboard.models.place.external_tag.ExtractedTag` rows.
Makes no API calls of its own - every function here reads a dict some other
gateway already produced.
"""

from __future__ import annotations

from urbanlens.dashboard.models.place.external_tag import ExtractedTag

#: OSM fields already parsed onto Nominatim's normalised dict that describe
#: what a place *is*, distinct from descriptive attributes (opening hours,
#: fees, accessibility, ...) which stay out of this feature entirely.
_NOMINATIM_SECONDARY_FIELDS: tuple[str, ...] = ("building", "amenity", "tourism", "historic")

#: Known boolean-ish OSM tag values, mapped to a friendlier label. Relocated
#: from ``services.apis.locations.nominatim`` (formerly private there).
_BOOLISH_VALUE_LABELS: dict[str, str] = {
    "yes": "Yes",
    "no": "No",
    "limited": "Limited",
    "designated": "Designated",
    "permissive": "Permissive",
    "private": "Private",
    "public": "Public",
    "customers": "Customers only",
    "only": "Only",
}


def humanize_tag_value(value: str) -> str:
    """Turn a raw provider tag value into a display-friendly string.

    Relocated from ``services.apis.locations.nominatim``'s private
    ``_humanize_osm_value`` (formerly used only for that panel's
    ``extra_details`` list) so it can also format values for the external-tag
    chips this module feeds. Storage always keeps the raw value; this is for
    display only.

    Args:
        value: Raw tag value, e.g. "limited" or "single_family_residential".

    Returns:
        A humanized value: known boolean-ish tokens map to a friendly label;
        anything else has underscores/semicolons turned into a
        comma-separated, space-joined string.
    """
    cleaned = value.strip()
    mapped = _BOOLISH_VALUE_LABELS.get(cleaned.lower())
    if mapped:
        return mapped
    return ", ".join(part.strip().replace("_", " ") for part in cleaned.split(";") if part.strip())


def extract_nominatim_tags(place_data: dict) -> list[ExtractedTag]:
    """The classification tags in a Nominatim ``reverse_geocode()`` result.

    Operates on the already-normalised dict shape
    ``NominatimGateway._normalise()`` returns (the same shape cached in
    ``LocationCache(source="nominatim").data``), not the raw API response, so
    this works identically whether called right after a fresh fetch or on a
    cache row read back later.

    Args:
        place_data: A normalised Nominatim result (or ``{}``/a partial dict).

    Returns:
        The primary ``category``/``type`` pair (OSM's own most-specific tag
        for this coordinate) marked primary, plus any of
        ``building``/``amenity``/``tourism``/``historic`` that are non-empty
        and not already identical to the primary pair - those fields usually
        just repeat the primary tag, so only genuinely additional
        information (e.g. ``tourism=museum`` alongside ``historic=yes``) is
        kept.
    """
    tags: list[ExtractedTag] = []
    primary_key = place_data.get("category") or ""
    primary_value = place_data.get("type") or ""
    if primary_key and primary_value:
        tags.append(ExtractedTag(key=primary_key, value=primary_value, is_primary=True))

    for field in _NOMINATIM_SECONDARY_FIELDS:
        value = place_data.get(field) or ""
        if value and (field, value) != (primary_key, primary_value):
            tags.append(ExtractedTag(key=field, value=value))

    return tags


def extract_overture_tags(attributes: dict) -> list[ExtractedTag]:
    """The classification tags in an Overture ``get_building_attributes()`` result.

    Never call this with the ``nearby_places``-merged dict
    ``OvertureBuildingAttributesPanelSource.fetch()`` builds for its cache
    row - ``nearby_places`` describes *other* points of interest near the
    coordinate, not this building, and has no place here.

    Args:
        attributes: The raw ``get_building_attributes()`` return dict (or
            ``{}``/a partial dict).

    Returns:
        ``subtype`` (the more specific of the two) marked primary when
        present, plus ``class_`` when present and distinct from ``subtype``
        - promoted to primary itself if ``subtype`` was missing, so a
        building with only a class still gets one primary tag rather than
        none.
    """
    tags: list[ExtractedTag] = []
    subtype = attributes.get("subtype") or ""
    building_class = attributes.get("class_") or ""
    if subtype:
        tags.append(ExtractedTag(key="building_subtype", value=subtype, is_primary=True))
    if building_class and building_class != subtype:
        tags.append(ExtractedTag(key="building_class", value=building_class, is_primary=not subtype))
    return tags
