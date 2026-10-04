"""The WKT and WKB readers an import uses: the same geometry GEOS reads, nothing nested past ``MAX_NESTING`` (P95)."""

from __future__ import annotations

import struct
from unittest import mock

import shapely
import shapely.errors

from hypothesis import example, given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.import_formats import geometry_readers
from urbanlens.dashboard.services.import_formats.geometry_readers import (
    MAX_NESTING,
    UnreadableGeometryError,
    read_wkb,
    read_wkt,
)

_FLOATS = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


def _read_long(text: str):
    """Through the long-line reader, whatever *text*'s length."""
    with mock.patch.object(geometry_readers, "GEOS_WKT_LINE_LIMIT", 0):
        return read_wkt(text)


def _exact(geometry) -> str:
    return f"{geometry.geom_type} {shapely.to_wkt(geometry, rounding_precision=-1, output_dimension=4)}"


@st.composite
def _numbers(draw, value: float) -> str:
    """*value* as one of the decimal spellings both readers take."""
    style = draw(st.sampled_from(("repr", "exponent", "plus", "fixed")))
    if style == "exponent":
        return f"{value:.17e}"
    if style == "plus" and value >= 0:
        return f"+{value!r}"
    if style == "fixed":
        return f"{value:.6f}"
    return repr(value)


@st.composite
def _coordinate(draw, ordinates: int) -> str:
    separator = draw(st.sampled_from((" ", "  ", "\t", " \n ")))
    return separator.join(draw(_numbers(draw(_FLOATS))) for _ in range(ordinates))


@st.composite
def _sequence(draw, ordinates: int, *, minimum: int = 2, closed: bool = False) -> str:
    coordinates = draw(st.lists(_coordinate(ordinates), min_size=minimum, max_size=6))
    if closed:
        coordinates.append(coordinates[0])
    comma = draw(st.sampled_from((",", ", ", " , ")))
    return "(" + comma.join(coordinates) + ")"


@st.composite
def _wkt(draw, ordinates: int, depth: int = 0) -> str:
    kind = draw(
        st.sampled_from(
            ("POINT", "LINESTRING", "POLYGON", "MULTIPOINT", "MULTILINESTRING", "MULTIPOLYGON")
            + (("GEOMETRYCOLLECTION",) if depth < 2 else ())
        )
    )
    keyword = draw(st.sampled_from((kind, kind.lower(), kind.title())))
    if draw(st.integers(0, 9)) == 0:
        return f"{keyword} EMPTY"
    if kind == "POINT":
        body = "(" + draw(_coordinate(ordinates)) + ")"
    elif kind == "LINESTRING":
        body = draw(_sequence(ordinates))
    elif kind == "POLYGON":
        body = (
            "(" + ", ".join(draw(st.lists(_sequence(ordinates, minimum=3, closed=True), min_size=1, max_size=3))) + ")"
        )
    elif kind == "MULTIPOINT":
        points = draw(st.lists(_coordinate(ordinates), min_size=1, max_size=5))
        # GEOS cuts an untagged bare four-number point to three; the reader refuses it (see the departures).
        bare = ordinates < 4 and draw(st.booleans())
        body = "(" + ", ".join(point if bare else f"({point})" for point in points) + ")"
    elif kind == "MULTILINESTRING":
        body = "(" + ", ".join(draw(st.lists(_sequence(ordinates), min_size=1, max_size=3))) + ")"
    elif kind == "MULTIPOLYGON":
        polygons = draw(st.lists(_sequence(ordinates, minimum=3, closed=True), min_size=1, max_size=3))
        body = "(" + ", ".join(f"({ring})" for ring in polygons) + ")"
    else:
        body = "(" + ", ".join(draw(st.lists(_wkt(ordinates, depth + 1), min_size=1, max_size=3))) + ")"
    return f"{keyword} {body}"


@st.composite
def _tagged_wkt(draw) -> str:
    ordinates = draw(st.sampled_from((2, 3, 4)))
    text = draw(_wkt(ordinates))
    tag = {2: "", 3: draw(st.sampled_from(("", " Z", " M"))), 4: draw(st.sampled_from(("", " ZM")))}[ordinates]
    if tag:
        keyword, rest = text.split(" ", 1)
        text = f"{keyword}{tag} {rest}"
    return text


class LongLineReaderTests(SimpleTestCase):
    @settings(max_examples=400)
    @given(_tagged_wkt())
    @example("LINESTRING (1 2, 3 4)")
    @example("MULTIPOINT (1 2, (3 4), EMPTY)")
    @example("GEOMETRYCOLLECTION (POINT EMPTY, LINESTRING Z (1 2 3, 4 5 6))")
    @example("POLYGON ((0 0, 1 0, 1 1, 0 0), EMPTY)")
    def test_it_reads_what_geos_reads(self, text: str) -> None:
        try:
            expected = shapely.from_wkt(text)
        except shapely.errors.ShapelyError:
            with self.assertRaises((shapely.errors.ShapelyError, UnreadableGeometryError)):
                _read_long(text)
            return

        self.assertEqual(_exact(_read_long(text)), _exact(expected))

    @settings(max_examples=400)
    @given(st.text(alphabet="POINTLESMUYGRAZ (),.0123456789-+eE \t", max_size=60))
    @example("LINESTRING (1 2)")
    @example("POLYGON ((0 0, 1 0, 1 1, 0 1))")
    @example("POINT (1 2, 3 4)")
    @example("LINESTRING (1 2 3, 4 5)")
    def test_what_it_reads_geos_reads_the_same(self, text: str) -> None:
        """It may refuse a dialect GEOS takes, but never accepts a different geometry or one GEOS would refuse."""
        try:
            ours = _read_long(text)
        except (shapely.errors.ShapelyError, UnreadableGeometryError):
            return

        self.assertEqual(_exact(ours), _exact(shapely.from_wkt(text)))

    def test_the_departures_it_documents(self) -> None:
        for text in (
            "POINT (inf 2)",
            "POINT (nan 2)",
            "POINT (0x1p3 2)",
            "POINT (1_000 2)",
            "POINTZ (1 2 3)",
            "SRID=4326;POINT (1 2)",
            "MULTIPOINT (1 2 3 4, 5 6 7 8)",
        ):
            with self.subTest(text=text), self.assertRaises(UnreadableGeometryError):
                _read_long(text)

    def test_a_line_under_the_limit_is_read_by_geos(self) -> None:
        """So the departures above are confined to lines over a megabyte."""
        self.assertEqual(read_wkt("POINT (inf 2)").x, float("inf"))

    def test_a_long_line_is_read(self) -> None:
        coordinates = ", ".join(f"{-74 - i / 1e5:.6f} {40 + i / 1e5:.6f}" for i in range(70_000))
        text = f"LINESTRING ({coordinates})"
        self.assertGreater(len(text), geometry_readers.GEOS_WKT_LINE_LIMIT)

        self.assertEqual(_exact(read_wkt(text)), _exact(shapely.from_wkt(text)))

    def test_a_point_off_the_number_line_is_refused(self) -> None:
        with self.assertRaises(UnreadableGeometryError):
            _read_long("POINT (1e400 2)")


def _nested_wkt(depth: int) -> str:
    return "GEOMETRYCOLLECTION (" * depth + "POINT (1 2)" + ")" * depth


def _nested_wkb(depth: int, *, little_endian: bool = True) -> bytes:
    order, prefix = (b"\x01", "<") if little_endian else (b"\x00", ">")
    return (order + struct.pack(f"{prefix}II", 7, 1)) * depth + order + struct.pack(f"{prefix}Idd", 1, 1, 2)


class NestingTests(SimpleTestCase):
    def test_wkt_nested_to_the_limit_is_read_and_past_it_refused(self) -> None:
        for reader in (read_wkt, _read_long):
            with self.subTest(reader=reader.__name__):
                # The point's own parentheses are one level.
                self.assertEqual(reader(_nested_wkt(MAX_NESTING - 1)).geom_type, "GeometryCollection")
                with self.assertRaises(UnreadableGeometryError):
                    reader(_nested_wkt(MAX_NESTING))

    def test_wkb_nested_to_the_limit_is_read_and_past_it_refused(self) -> None:
        for little_endian in (True, False):
            with self.subTest(little_endian=little_endian):
                self.assertEqual(
                    read_wkb(_nested_wkb(MAX_NESTING, little_endian=little_endian)).geom_type, "GeometryCollection"
                )
                with self.assertRaises(UnreadableGeometryError):
                    read_wkb(_nested_wkb(MAX_NESTING + 1, little_endian=little_endian))

    def test_many_shallow_parentheses_are_not_depth(self) -> None:
        polygons = ", ".join("((0 0, 1 0, 1 1, 0 0))" for _ in range(MAX_NESTING * 3))

        self.assertEqual(read_wkt(f"MULTIPOLYGON ({polygons})").geom_type, "MultiPolygon")

    @given(
        st.lists(
            st.builds(shapely.Point, _FLOATS, _FLOATS)
            | st.builds(shapely.LineString, st.lists(st.tuples(_FLOATS, _FLOATS), min_size=2, max_size=5))
            | st.builds(lambda x, y: shapely.box(x, y, x + 1, y + 1), _FLOATS, _FLOATS),
            min_size=1,
            max_size=4,
        ),
        st.booleans(),
        st.booleans(),
        st.sampled_from(("iso", "extended")),
    )
    def test_the_wkb_walk_reads_every_shape_geos_writes(
        self, parts, three_d: bool, little_endian: bool, flavor
    ) -> None:
        geometry = shapely.GeometryCollection([shapely.MultiPoint([(0, 0), (1, 1)]), *parts])
        if three_d:
            geometry = shapely.force_3d(geometry, 7.5)
        if flavor == "extended":
            geometry = shapely.set_srid(geometry, 4326)
        data = shapely.to_wkb(geometry, flavor=flavor, include_srid=flavor == "extended", byte_order=int(little_endian))

        self.assertTrue(shapely.equals_exact(read_wkb(data), geometry, tolerance=0))

    def test_a_curve_is_refused_as_unreadable(self) -> None:
        """Shapely 2.1 holds no curve; its own reader raises NotImplementedError, which an import does not expect."""
        curve = b"\x01" + struct.pack("<II6d", 8, 3, 0, 0, 1, 1, 2, 0)
        for read in (
            lambda: read_wkt("CIRCULARSTRING (0 0, 1 1, 2 0)"),
            lambda: _read_long("CIRCULARSTRING (0 0, 1 1, 2 0)"),
            lambda: read_wkb(curve),
        ):
            with self.subTest(read=read), self.assertRaises(UnreadableGeometryError):
                read()

    def test_a_wkb_shorter_than_its_counts_is_refused(self) -> None:
        line = shapely.to_wkb(shapely.LineString([(0, 0), (1, 1), (2, 2)]))
        with self.assertRaises(UnreadableGeometryError):
            read_wkb(line[:-8])

    def test_a_wkb_type_geos_has_no_reader_for_is_refused(self) -> None:
        with self.assertRaises(UnreadableGeometryError):
            read_wkb(b"\x01" + struct.pack("<II", 15, 0))
