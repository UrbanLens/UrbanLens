"""OSM XML pin import.
Only elements carrying at least one ``<tag>`` become pins - most nodes in an OSM XML export are untagged geometry vertices belonging to a way, not points of interest in their own right."""

from __future__ import annotations

import logging
from typing import IO, TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, iterparse

from urbanlens.dashboard.services.import_formats.heuristics import pick_name_and_description
from urbanlens.dashboard.services.import_formats.streams import as_stream
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Iterator

    # Only used for type checking
    from xml.etree.ElementTree import Element  # nosec B405

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)

_WAY_REFS_PER_PASS = 100_000

type _Way = tuple[str | None, dict[str, str], list[str | None]]


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


@untrusted_parse("geo.osm_xml")
def osm_xml_to_dict(file_contents: bytes | IO[bytes], user_profile: Profile) -> list[dict[str, Any]]:
    """Convert tagged OSM XML nodes and ways into pin dicts.

    Args:
        file_contents: The OSM XML file, as bytes or a seekable binary file positioned at its start.
        user_profile: The profile to associate with each pin.

    Returns:
        List of pin dicts, one per tagged node and one per tagged way (way pins are placed at the centroid of the way's referenced node coordinates).

    Raises:
        xml.etree.ElementTree.ParseError: If the file is not valid XML.
        ValueError: If a ``lat``/``lon`` attribute cannot be parsed as a float."""
    try:
        pins = list(iter_osm_xml_pins(file_contents, user_profile))
    except (ParseError, ValueError) as e:
        logger.exception("Failed to import pins from OSM XML: %s", e)
        raise
    logger.debug("Converted %s tagged nodes/ways from OSM XML to pins.", len(pins))
    return pins


@untrusted_parse("geo.osm_xml")
def iter_osm_xml_pins(file_contents: bytes | IO[bytes], user_profile: Profile) -> Iterator[dict[str, Any]]:
    """:func:`osm_xml_to_dict`, reading the file an element at a time rather than building its tree.

    Every tagged node comes before every tagged way, in document order, as with the tree. The nodes are read in a
    pass that keeps none of them. The ways are then read a batch at a time, each batch in two passes - one taking
    the next ways' node references, one finding those nodes - so no pass holds every node's coordinates, and none
    is made once the caller stops asking.

    Args:
        file_contents: The OSM XML file, as bytes or a seekable binary file positioned at its start.
        user_profile: The profile to associate with each pin.

    Yields:
        One pin dict per tagged node, then one per tagged way.

    Raises:
        xml.etree.ElementTree.ParseError: If the file is not valid XML, once reading reaches the fault.
        ValueError: If a ``lat``/``lon`` attribute cannot be parsed as a float."""
    stream = as_stream(file_contents)
    start = stream.tell()
    for node in _top_level(stream, "node"):
        located = _located(node)
        tags = _tags(node)
        if located is not None and tags:
            node_id, (lat, lon) = located
            yield _pin_from_tags(tags, lat, lon, f"OSM node {node_id}", user_profile)

    taken = 0
    while True:
        stream.seek(start)
        ways, last = _tagged_ways(stream, skip=taken)
        taken += len(ways)
        stream.seek(start)
        coords_of = _coords_of(stream, {ref for _, _, refs in ways for ref in refs if ref is not None})
        for way_id, tags, refs in ways:
            coords = [coords_of[ref] for ref in refs if ref is not None and ref in coords_of]
            if not refs or len(coords) != len(refs):
                logger.warning("Skipping way %s: one or more referenced nodes are missing coordinates.", way_id)
                continue
            centroid_lat = sum(c[0] for c in coords) / len(coords)
            centroid_lon = sum(c[1] for c in coords) / len(coords)
            yield _pin_from_tags(tags, centroid_lat, centroid_lon, f"OSM way {way_id}", user_profile)
        if last:
            return


def _located(node: Element) -> tuple[str, tuple[float, float]] | None:
    """A node's id and coordinates, or None when it lacks one of them."""
    node_id, lat, lon = node.get("id"), node.get("lat"), node.get("lon")
    if node_id is None or lat is None or lon is None:
        return None
    return node_id, (float(lat), float(lon))


def _tagged_ways(stream: IO[bytes], *, skip: int) -> tuple[list[_Way], bool]:
    """The tagged ways after the first *skip*, until they reference ``_WAY_REFS_PER_PASS`` nodes.

    Args:
        stream: The OSM XML file, positioned at its start.
        skip: How many tagged ways earlier batches took.

    Returns:
        Each way's id, tags and node references, and whether the file holds no more.
    """
    ways: list[_Way] = []
    refs_taken = 0
    for way in _top_level(stream, "way"):
        tags = _tags(way)
        if not tags:
            continue
        if skip:
            skip -= 1
            continue
        refs = [nd.get("ref") for nd in way.findall("nd")]
        ways.append((way.get("id"), tags, refs))
        refs_taken += len(refs)
        if refs_taken >= _WAY_REFS_PER_PASS:
            return ways, False
    return ways, True


def _coords_of(stream: IO[bytes], wanted: set[str]) -> dict[str, tuple[float, float]]:
    """The coordinates of each wanted node, the last one wherever an id repeats, as a tree's index of them was."""
    found: dict[str, tuple[float, float]] = {}
    if not wanted:
        return found
    for node in _top_level(stream, "node"):
        located = _located(node)
        if located is not None and located[0] in wanted:
            found[located[0]] = located[1]
    return found


def _top_level(stream: IO[bytes], tag: str) -> Iterator[Element]:
    """Each child of the root element named *tag*, complete, freeing every child of the root once it closes."""
    depth = 0
    root: Element | None = None
    for event, element in iterparse(stream, events=("start", "end")):
        if event == "start":
            depth += 1
            if root is None:
                root = element
            continue
        depth -= 1
        if depth != 1 or root is None:
            continue
        if element.tag == tag:
            yield element
        root.remove(element)
