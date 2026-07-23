"""OSM XML pin import.

Parses the ``<node>``/``<way>`` elements produced by an Overpass Turbo export (the
common way users pull "all abandoned:* tagged features within X radius" style
queries). Only elements carrying at least one ``<tag>`` become pins - most nodes in
an OSM XML export are untagged geometry vertices belonging to a way, not points of
interest in their own right.

``<relation>`` elements (multipolygons, administrative boundaries, and other
multi-way groupings) are intentionally out of scope: correctly resolving a relation
requires role-aware member resolution, which is disproportionate to this format's
purpose here of pulling individual tagged point/building features rather than
rendering a full OSM dataset. This is a deliberate limitation, not a gap to fill.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, fromstring as parse_xml

from urbanlens.dashboard.services.import_formats.heuristics import pick_name_and_description

if TYPE_CHECKING:
    # Only used for type checking
    from xml.etree.ElementTree import Element  # nosec B405

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)


def _tags(element: Element) -> dict[str, str]:
    """Return a flat ``{k: v}`` dict from an element's ``<tag k="..." v="..."/>`` children."""
    return {tag.get("k", ""): tag.get("v", "") for tag in element.findall("tag") if tag.get("k")}


def _pin_from_tags(tags: dict[str, str], lat: float, lon: float, fallback_name: str, user_profile: Profile) -> dict[str, Any]:
    """Build a pin dict from an OSM element's tags and resolved coordinates."""
    name, description = pick_name_and_description(tags, fallback_name=fallback_name)
    return {
        "latitude": lat,
        "longitude": lon,
        "profile": user_profile,
        "name": name,
        "description": description,
    }


def osm_xml_to_dict(file_contents: bytes, user_profile: Profile) -> list[dict[str, Any]]:
    """Convert tagged OSM XML nodes and ways into pin dicts.

    Args:
        file_contents: Raw OSM XML file bytes.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per tagged node and one per tagged way (way pins
        are placed at the centroid of the way's referenced node coordinates).

    Raises:
        xml.etree.ElementTree.ParseError: If the file is not valid XML.
        ValueError: If a ``lat``/``lon`` attribute cannot be parsed as a float.
    """
    pins: list[dict[str, Any]] = []
    try:
        root = parse_xml(file_contents)

        node_coords: dict[str, tuple[float, float]] = {}
        for node in root.findall("node"):
            node_id, lat, lon = node.get("id"), node.get("lat"), node.get("lon")
            if node_id is None or lat is None or lon is None:
                continue
            node_coords[node_id] = (float(lat), float(lon))

        for node in root.findall("node"):
            node_id = node.get("id")
            tags = _tags(node)
            if not tags or node_id not in node_coords:
                continue
            lat, lon = node_coords[node_id]
            pins.append(_pin_from_tags(tags, lat, lon, f"OSM node {node_id}", user_profile))

        for way in root.findall("way"):
            tags = _tags(way)
            if not tags:
                continue
            way_id = way.get("id")
            refs = [nd.get("ref") for nd in way.findall("nd")]
            coords = [node_coords[ref] for ref in refs if ref is not None and ref in node_coords]
            if not refs or len(coords) != len(refs):
                logger.warning("Skipping way %s: one or more referenced nodes are missing coordinates.", way_id)
                continue
            centroid_lat = sum(c[0] for c in coords) / len(coords)
            centroid_lon = sum(c[1] for c in coords) / len(coords)
            pins.append(_pin_from_tags(tags, centroid_lat, centroid_lon, f"OSM way {way_id}", user_profile))

        logger.debug("Converted %s tagged nodes/ways from OSM XML to pins.", len(pins))
    except (ParseError, ValueError) as e:
        logger.exception("Failed to import pins from OSM XML: %s", e)
        raise

    return pins
