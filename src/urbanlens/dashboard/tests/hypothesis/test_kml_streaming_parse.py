"""The KML import streams placemarks, and reads exactly what the fastkml tree walk it replaced read (P95).

fastkml built a full lxml-backed tree before reading one placemark: about fifteen times the file in memory.
"""

from __future__ import annotations

import html
import re
import tracemalloc
from typing import Any

from fastkml import kml

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway, _kml_tuples

_NAMESPACE_RE = re.compile(rb"https://((?:www\.)?(?:opengis\.net|google\.com/kml|w3\.org)/)")


def _fastkml_oracle(content: bytes) -> list[tuple[Any, Any, float, float]]:
    """The algorithm the streaming parser replaced, kept here as the reference it must agree with."""

    def walk(features: Any) -> Any:
        for feature in features:
            if isinstance(feature, kml.Placemark):
                yield feature
            elif getattr(feature, "features", None):
                yield from walk(feature.features)

    parsed = kml.KML.from_string(_NAMESPACE_RE.sub(rb"http://\1", content))  # type: ignore[arg-type]
    rows = []
    for placemark in walk(parsed.features):
        geometry = placemark.geometry
        if geometry is None or not hasattr(geometry, "coords"):
            continue
        lon, lat = next(iter(geometry.coords))[:2]
        rows.append((placemark.name, placemark.description, lat, lon))
    return rows


def _parse(content: bytes) -> list[tuple[Any, Any, float, float]]:
    pins = GoogleMapsGateway().takeout_kml_to_dict(content, user_profile=None)  # type: ignore[arg-type]
    return [(pin["name"], pin["description"], pin["latitude"], pin["longitude"]) for pin in pins]


_text = (
    st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=20)
    .map(str.strip)
    .filter(bool)
)
_coord = st.tuples(st.floats(-179, 179, allow_nan=False), st.floats(-89, 89, allow_nan=False)).map(
    lambda c: (round(c[0], 5), round(c[1], 5))
)


@st.composite
def _placemark(draw: Any) -> str:
    parts = []
    if draw(st.booleans()):
        parts.append(f"<name>{html.escape(draw(_text))}</name>")
    if draw(st.booleans()):
        text = draw(_text)
        parts.append(
            f"<description><![CDATA[{text.replace(']]>', '')}]]></description>"
            if draw(st.booleans())
            else f"<description>{html.escape(text)}</description>"
        )
    kind = draw(st.sampled_from(["Point", "LineString", "none"]))
    # fastkml's geometry library dropped a line mixing 2D and 3D points, or repeating one, and the placemark with it.
    altitude = ",12" if draw(st.booleans()) else ""
    coords = " ".join(
        f"{lon},{lat}{altitude}" for lon, lat in draw(st.lists(_coord, min_size=1, max_size=4, unique=True))
    )
    if kind in {"Point", "LineString"}:
        if kind == "Point":
            coords = coords.split(" ")[0]
        parts.append(f"<{kind}><coordinates>\n  {coords}\n</coordinates></{kind}>")
    return "<Placemark>" + "".join(parts) + "</Placemark>"


@st.composite
def _document(draw: Any) -> bytes:
    placemarks = draw(st.lists(_placemark(), max_size=6))
    body = ""
    for placemark in placemarks:
        body += f"<Folder><name>Layer</name>{placemark}</Folder>" if draw(st.booleans()) else placemark
    scheme = draw(st.sampled_from(["http", "https"]))
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="{scheme}://www.opengis.net/kml/2.2"><Document><name>Doc</name>{body}</Document></kml>'.encode()


class StreamingKmlAgreesWithFastkmlTests(SimpleTestCase):
    """Over what fastkml could read: it crashed on a Polygon or MultiGeometry (see the tests below)."""

    @hyp_settings(max_examples=150, deadline=None)
    @given(_document())
    def test_every_generated_document_reads_the_same(self, content: bytes) -> None:
        # fastkml returned a Document's Folders before its own Placemarks; the stream keeps document order.
        self.assertCountEqual(_parse(content), _fastkml_oracle(content))

    def test_a_placemark_directly_under_kml_and_one_deep_in_folders_are_both_read(self) -> None:
        content = (
            b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><Folder>'
            b"<Placemark><name>Deep</name><Point><coordinates>-73.7,42.6</coordinates></Point></Placemark>"
            b"</Folder></Folder></Document></kml>"
        )
        self.assertEqual(_parse(content), [("Deep", None, 42.6, -73.7)])


class CoordinateFormattingTests(SimpleTestCase):
    """What fastkml tolerated, the stream must too, or one placemark fails the whole file."""

    def test_a_space_after_the_comma_reads_like_fastkml(self) -> None:
        content = (
            b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            b"<Placemark><name>  Padded  </name><description> Text </description><Point><coordinates>-73.7, 42.6</coordinates></Point></Placemark>"
            b"</Document></kml>"
        )
        self.assertEqual(_parse(content), _fastkml_oracle(content))
        self.assertEqual(_parse(content), [("Padded", "Text", 42.6, -73.7)])

    def test_a_space_before_the_comma_is_read_where_fastkml_dropped_the_placemark(self) -> None:
        content = (
            b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            b"<Placemark><LineString><coordinates>-73.7 , 42.6 , 10  -73.8,42.7</coordinates></LineString></Placemark>"
            b"</Document></kml>"
        )
        self.assertEqual(_parse(content), [(None, None, 42.6, -73.7)])


class AreaPlacemarkTests(SimpleTestCase):
    """A Polygon or MultiGeometry raised past the importer's error tuple, failing the whole file. It is read at its
    centroid now, as the GeoJSON importer reads the same shapes."""

    def _doc(self, geometry: str) -> bytes:
        return f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Area</name>{geometry}</Placemark></Document></kml>'.encode()

    def test_a_polygon_is_read_at_its_centroid(self) -> None:
        ring = "0,0 4,0 4,2 0,2 0,0"
        pins = _parse(
            self._doc(
                f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            )
        )
        self.assertEqual(pins, [("Area", None, 1.0, 2.0)])

    def test_a_multigeometry_is_read_at_the_centroid_of_its_parts(self) -> None:
        pins = _parse(
            self._doc(
                "<MultiGeometry><Point><coordinates>0,0</coordinates></Point><Point><coordinates>2,4</coordinates></Point></MultiGeometry>"
            )
        )
        self.assertEqual(pins, [("Area", None, 2.0, 1.0)])

    def test_a_placemark_with_no_readable_geometry_is_skipped(self) -> None:
        self.assertEqual(
            _parse(self._doc("<gx:Track xmlns:gx='http://www.google.com/kml/ext/2.2'><when>2020</when></gx:Track>")), []
        )


class StreamingKmlMemoryTests(SimpleTestCase):
    def test_memory_does_not_grow_with_the_number_of_placemarks(self) -> None:
        placemark = b"<Placemark><name>Place %d</name><description>A description of it</description><Point><coordinates>-73.75,42.65,0</coordinates></Point></Placemark>"
        content = (
            b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            + b"".join(placemark % i for i in range(60_000))
            + b"</Document></kml>"
        )

        tracemalloc.start()
        try:
            pins = GoogleMapsGateway().takeout_kml_to_dict(content, user_profile=None)  # type: ignore[arg-type]
            kept, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertEqual(len(pins), 60_000)
        overhead = peak - kept
        self.assertLess(
            overhead,
            len(content) // 2,
            f"parsing a {len(content):,}-byte file needed {overhead:,} bytes beyond the pins it returned",
        )


class CoordinateTuplesTests(SimpleTestCase):
    """P95: the tuples of a ``coordinates`` text are found without a substitution over the whole text."""

    @given(st.lists(st.sampled_from(["1", "2.5", "-3", ",", ",,", " ", "  ", "\n", "\t", "\xa0", " ", "x"])))
    def test_the_tuples_are_those_of_collapsing_the_spaces_around_each_comma(self, pieces: list[str]) -> None:
        text = "".join(pieces)

        self.assertEqual(list(_kml_tuples(text)), re.sub(r"\s*,\s*", ",", text).split())
