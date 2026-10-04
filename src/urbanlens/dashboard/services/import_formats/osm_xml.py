"""OSM XML pin import.
Only elements carrying at least one ``<tag>`` become pins - most nodes in an OSM XML export are untagged geometry vertices belonging to a way, not points of interest in their own right."""

from __future__ import annotations

from array import array
import logging
from typing import IO, TYPE_CHECKING, Any

from defusedxml.ElementTree import ParseError, iterparse

from urbanlens.dashboard.services.import_formats.heuristics import pick_name_and_description
from urbanlens.dashboard.services.import_formats.streams import as_stream
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    # Only used for type checking
    from xml.etree.ElementTree import Element  # nosec B405

    from urbanlens.dashboard.models.profile import Profile

logger = logging.getLogger(__name__)

_WAY_REFS_PER_PASS = 250_000

type _Key = int | str
type _Way = tuple[str | None, dict[str, str], list[_Key | None]]


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
        coordinates = _Coordinates(stream, (ref for _, _, refs in ways for ref in refs if ref is not None))
        for way_id, tags, refs in ways:
            places = coordinates.places(refs)
            if not places:
                logger.warning("Skipping way %s: one or more referenced nodes are missing coordinates.", way_id)
                continue
            # sum() of the same floats in the same order, so the centroid is the one a list of them gave.
            centroid_lat = sum(coordinates.lats[place] for place in places) / len(places)
            centroid_lon = sum(coordinates.lons[place] for place in places) / len(places)
            yield _pin_from_tags(tags, centroid_lat, centroid_lon, f"OSM way {way_id}", user_profile)
        if last:
            return


def _located(node: Element) -> tuple[str, tuple[float, float]] | None:
    """A node's id and coordinates, or None when it lacks one of them."""
    node_id, lat, lon = node.get("id"), node.get("lat"), node.get("lon")
    if node_id is None or lat is None or lon is None:
        return None
    return node_id, (float(lat), float(lon))


def _key(node_id: str) -> _Key:
    """A node id as an int when it is spelled as one canonically, else as itself, so two keys match when the ids do."""
    if node_id.isascii() and node_id.lstrip("-").isdigit():
        number = int(node_id)
        if str(number) == node_id:
            return number
    return node_id


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
    for way_id, tags, refs in _top_level_ways(stream):
        if not tags:
            continue
        if skip:
            skip -= 1
            continue
        ways.append((way_id, tags, refs))
        refs_taken += len(refs)
        if refs_taken >= _WAY_REFS_PER_PASS:
            return ways, False
    return ways, True


class _Coordinates:
    """The coordinates of each wanted node, the last one wherever an id repeats, as a tree's index of them was."""

    def __init__(self, stream: IO[bytes], wanted: Iterable[_Key]) -> None:
        #: Each wanted node's place in ``lats`` and ``lons``; None until the node is found.
        self.index: dict[_Key, int | None] = dict.fromkeys(wanted)
        self.lats = array("d")
        self.lons = array("d")
        if not self.index:
            return
        for node in _top_level(stream, "node"):
            located = _located(node)
            if located is None:
                continue
            key = _key(located[0])
            if key in self.index:
                self.index[key] = len(self.lats)
                self.lats.append(located[1][0])
                self.lons.append(located[1][1])

    def places(self, refs: list[_Key | None]) -> list[int] | None:
        """Where each reference's node sits in ``lats`` and ``lons``, or None when any of them was not found."""
        places = []
        for ref in refs:
            place = None if ref is None else self.index.get(ref)
            if place is None:
                return None
            places.append(place)
        return places


def _top_level_ways(stream: IO[bytes]) -> Iterator[_Way]:
    """Each way's id, tags and node references, its children read and freed as they close rather than kept to its end.

    The tags and references are the way's own ``<tag>`` and ``<nd>`` children, as ``findall`` finds them.
    """
    depth = 0
    root: Element | None = None
    way: Element | None = None
    tags: dict[str, str] = {}
    refs: list[_Key | None] = []
    for event, element in iterparse(stream, events=("start", "end")):
        if event == "start":
            depth += 1
            if root is None:
                root = element
            elif depth == 2 and element.tag == "way":
                way, tags, refs = element, {}, []
            continue
        depth -= 1
        if depth == 2 and way is not None:
            if element.tag == "nd":
                ref = element.get("ref")
                refs.append(None if ref is None else _key(ref))
            elif element.tag == "tag" and element.get("k"):
                tags[element.get("k", "")] = element.get("v", "")
            way.remove(element)
        elif depth == 1 and root is not None:
            if element is way:
                yield way.get("id"), tags, refs
                way = None
            root.remove(element)


def _top_level(stream: IO[bytes], tag: str) -> Iterator[Element]:
    """Each child of the root element named *tag*, complete, freeing every child of the root once it closes.

    A child of another name is never wanted whole, so its own children are freed as they close: one way through a
    file's every node costs a pass for nodes nothing per reference.
    """
    depth = 0
    root: Element | None = None
    unwanted: Element | None = None
    for event, element in iterparse(stream, events=("start", "end")):
        if event == "start":
            depth += 1
            if root is None:
                root = element
            elif depth == 2 and element.tag != tag:
                unwanted = element
            continue
        depth -= 1
        if depth == 2 and unwanted is not None:
            unwanted.remove(element)
            continue
        if depth != 1 or root is None:
            continue
        if element.tag == tag:
            yield element
        unwanted = None
        root.remove(element)
