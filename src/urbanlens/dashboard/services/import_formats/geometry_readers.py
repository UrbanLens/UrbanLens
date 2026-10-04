"""Reading an import's WKT and WKB without handing GEOS what it cannot read safely.

GEOS reads both formats recursively, so tens of thousands of nested collections overflow its stack and kill the
process. WKT whose parentheses nest deeper than :data:`MAX_NESTING`, or WKB whose collections do, is refused before GEOS
sees it.

GEOS's WKT reader also costs about nine times a line's length (P95). A line longer than :data:`GEOS_WKT_LINE_LIMIT` is
read here instead, into WKB, which GEOS reads at about its own size. That reader takes a strict subset of WKT; a long
line outside it is refused, where GEOS might have read it:

- a type, optionally ``Z``, ``M`` or ``ZM`` after a space, then ``EMPTY`` or parentheses;
- every coordinate of one geometry with the same number of ordinates: two or three, or four when tagged ``ZM`` or
  given four, except an untagged multipoint of bare points, which GEOS cuts to three;
- finite decimal numbers only: no ``inf``, ``nan``, hexadecimal or digit separators.
"""

from __future__ import annotations

import io
import re
import struct
from typing import TYPE_CHECKING, Final

import numpy as np
import shapely

if TYPE_CHECKING:
    from collections.abc import Callable

    from shapely.geometry.base import BaseGeometry

#: How deep WKT's parentheses, or WKB's collections, may nest. GEOS read ten thousand and crashed at thirty thousand;
#: real data nests a few deep.
MAX_NESTING: Final = 100

#: A WKT line longer than this is read by :class:`_WktToWkb` rather than GEOS's own reader.
GEOS_WKT_LINE_LIMIT: Final = 1 << 20

#: How much of a coordinate list is parsed at once, so a long one is never held as many small strings.
_CHUNK_CHARACTERS: Final = 1 << 18


class UnreadableGeometryError(ValueError):
    """A WKT or WKB geometry refused before GEOS reads it."""


def read_wkt(line: str) -> BaseGeometry:
    """A WKT line as a geometry.

    Args:
        line: One WKT geometry.

    Returns:
        The geometry.

    Raises:
        UnreadableGeometryError: It nests too deeply, is a curve, or is a long line outside the subset this module reads.
        shapely.errors.ShapelyError: GEOS refused it.
    """
    if len(line) > GEOS_WKT_LINE_LIMIT:
        return shapely.from_wkb(_WktToWkb(line).read())
    _refuse_deep_wkt(line)
    try:
        return shapely.from_wkt(line)
    except NotImplementedError as exc:
        # A curve: GEOS reads one, but shapely has no geometry to hold it.
        raise UnreadableGeometryError(str(exc)) from exc


def read_wkb(data: bytes) -> BaseGeometry:
    """A WKB geometry, once its nesting is known to be shallow enough for GEOS.

    Args:
        data: One WKB or EWKB geometry.

    Returns:
        The geometry.

    Raises:
        UnreadableGeometryError: It nests too deeply, is shorter than its own counts say, or holds a curve or a type GEOS
            has no reader for.
        shapely.errors.ShapelyError: GEOS refused it.
    """
    _refuse_deep_wkb(data)
    return shapely.from_wkb(data)


def _refuse_deep_wkt(line: str) -> None:
    if line.count("(") <= MAX_NESTING:
        return
    raw = np.frombuffer(line.encode(), dtype=np.uint8)
    steps = (raw == ord("(")).astype(np.int32) - (raw == ord(")"))
    if int(np.cumsum(steps).max()) > MAX_NESTING:
        raise UnreadableGeometryError(f"the geometry nests more than {MAX_NESTING} deep")


#: WKB type codes whose body is a count of geometries, each with its own header.
_WKB_COLLECTIONS: Final = frozenset({4, 5, 6, 7})
_WKB_LINESTRING: Final = 2
#: GEOS reads curves, but shapely has no geometry to hold one and raises ``NotImplementedError``.
_WKB_CURVES: Final = frozenset({8, 9, 10, 11, 12})
_WKB_POINT: Final = 1
_WKB_POLYGON: Final = 3
_EWKB_Z: Final = 0x80000000
_EWKB_M: Final = 0x40000000
_EWKB_SRID: Final = 0x20000000


class _WkbCursor:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> int:
        start = self.pos
        self.pos += size
        if self.pos > len(self.data):
            raise UnreadableGeometryError("the WKB is shorter than its own counts")
        return start

    def count(self, little_endian: bool) -> int:
        start = self.take(4)
        return struct.unpack_from("<I" if little_endian else ">I", self.data, start)[0]

    def header(self) -> tuple[int, int, bool]:
        """The next geometry's base type, ordinates per point and byte order."""
        order = self.data[self.take(1)]
        if order not in (0, 1):
            raise UnreadableGeometryError("the WKB names no byte order")
        little_endian = order == 1
        code = self.count(little_endian)
        if code & _EWKB_SRID:
            self.take(4)
        ordinates = 2 + bool(code & _EWKB_Z) + bool(code & _EWKB_M)
        code &= 0x0FFFFFFF
        iso_dimensions, base = divmod(code, 1000)
        if iso_dimensions > 3:
            raise UnreadableGeometryError("the WKB names no geometry type")
        ordinates = max(ordinates, 2 + (iso_dimensions in (1, 3)) + (iso_dimensions in (2, 3)))
        return base, ordinates, little_endian


def _refuse_deep_wkb(data: bytes) -> None:
    cursor = _WkbCursor(data)
    open_counts: list[int] = []
    while True:
        base, ordinates, little_endian = cursor.header()
        point_size = 8 * ordinates
        if base in _WKB_COLLECTIONS:
            parts = cursor.count(little_endian)
            if parts:
                if len(open_counts) >= MAX_NESTING:
                    raise UnreadableGeometryError(f"the geometry nests more than {MAX_NESTING} deep")
                open_counts.append(parts)
                continue
        elif base == _WKB_POINT:
            cursor.take(point_size)
        elif base == _WKB_LINESTRING:
            cursor.take(point_size * cursor.count(little_endian))
        elif base == _WKB_POLYGON:
            for _ring in range(cursor.count(little_endian)):
                cursor.take(point_size * cursor.count(little_endian))
        elif base in _WKB_CURVES:
            raise UnreadableGeometryError(f"WKB type {base} is a curve")
        else:
            raise UnreadableGeometryError(f"WKB type {base} is not one GEOS reads")
        while open_counts:
            open_counts[-1] -= 1
            if open_counts[-1]:
                break
            open_counts.pop()
        else:
            return


_NUMBER: Final = r"[+-]?+(?:\d++(?:\.\d*+)?+|\.\d++)(?:[eE][+-]?+\d++)?+"
_WORD: Final = re.compile(r"\s*+([A-Za-z]++)")
_PUNCTUATION: Final = re.compile(r"\s*+([(),])")
_SPACE: Final = re.compile(r"\s*+")
_COORDINATES: Final = {ordinates: re.compile(rf"\s*+{_NUMBER}(?:\s++{_NUMBER}){{{ordinates - 1}}}\s*+(?:,\s*+{_NUMBER}(?:\s++{_NUMBER}){{{ordinates - 1}}}\s*+)*+") for ordinates in (2, 3, 4)}
_NUMBER_PATTERN: Final = re.compile(_NUMBER)
_BARE_POINT: Final = re.compile(r"[^,)]*+")
_PARENTHESIZED_MEMBERS: Final = re.compile(r"\s*+\(\s*+(?:\(|(?i:EMPTY)(?![A-Za-z]))")

_TYPES: Final = {
    "POINT": 1,
    "LINESTRING": 2,
    "POLYGON": 3,
    "MULTIPOINT": 4,
    "MULTILINESTRING": 5,
    "MULTIPOLYGON": 6,
    "GEOMETRYCOLLECTION": 7,
}
_COLLECTION: Final = 7
_TAGS: Final = {"Z": (True, False), "M": (False, True), "ZM": (True, True)}
#: The first coordinate ahead, or the next geometry's type when this one holds none.
_FIRST_COORDINATE: Final = re.compile(rf"(?P<coordinate>{_NUMBER}(?:\s++{_NUMBER}){{0,3}}+)\s*+[,)]|(?P<type>(?i:{'|'.join(sorted(_TYPES, key=len, reverse=True))}))")


class _WktToWkb:
    """One WKT geometry as little-endian ISO WKB, read without a token object per number.

    Dimensions follow GEOS's WKT reader. A geometry takes its tag's ordinates, or else its first coordinate's; one with
    no coordinate, and an untagged collection, is two-dimensional. Each part of a multi-geometry has its owner's. A
    collection's members each take their own: under a tagged collection they must match its tag, and under an untagged
    one GEOS's WKB reader gives the collection their union.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.out = io.BytesIO()
        self.z = self.m = False
        self.ordinates = 2
        self.tagged = False
        self.depth = 0

    def read(self) -> bytes:
        self._geometry(required=None)
        if _SPACE.fullmatch(self.text, self.pos) is None:
            raise UnreadableGeometryError("text follows the geometry")
        return self.out.getvalue()

    def _geometry(self, *, required: tuple[bool, bool] | None) -> None:
        """A geometry with its own type keyword; *required* is a tagged collection's ordinates, which it must have."""
        base = self._type()
        tag = self._tag()
        if tag is not None:
            z, m = _TAGS[tag]
        elif base == _COLLECTION:
            z = m = False
        else:
            z, m = self._inferred()
        if required is not None and (z, m) != required:
            raise UnreadableGeometryError("a member has other ordinates than its collection")
        owner = (self.z, self.m, self.ordinates, self.tagged)
        self.z, self.m, self.ordinates, self.tagged = z, m, 2 + z + m, tag is not None
        members_required = (z, m) if tag is not None else None
        self._body(base, members_required)
        self.z, self.m, self.ordinates, self.tagged = owner

    def _inferred(self) -> tuple[bool, bool]:
        if self._empty_ahead():
            return False, False
        first = _FIRST_COORDINATE.search(self.text, self.pos)
        if first is None or first.group("coordinate") is None:
            return False, False
        numbers = len(_NUMBER_PATTERN.findall(first.group("coordinate")))
        if numbers not in _COORDINATES:
            raise UnreadableGeometryError("expected coordinates of two to four numbers")
        return numbers >= 3, numbers == 4

    def _type(self) -> int:
        word = self._word()
        if word is None or word.upper() not in _TYPES:
            raise UnreadableGeometryError("expected a geometry type")
        return _TYPES[word.upper()]

    def _tag(self) -> str | None:
        match = _WORD.match(self.text, self.pos)
        if match is None or match.group(1).upper() not in _TAGS:
            return None
        self.pos = match.end()
        return match.group(1).upper()

    def _word(self) -> str | None:
        match = _WORD.match(self.text, self.pos)
        if match is None:
            return None
        self.pos = match.end()
        return match.group(1)

    def _empty_ahead(self) -> bool:
        match = _WORD.match(self.text, self.pos)
        return match is not None and match.group(1).upper() == "EMPTY"

    def _empty(self) -> bool:
        if not self._empty_ahead():
            return False
        self._word()
        return True

    def _punctuation(self) -> str | None:
        match = _PUNCTUATION.match(self.text, self.pos)
        return match.group(1) if match else None

    def _expect(self, character: str) -> None:
        match = _PUNCTUATION.match(self.text, self.pos)
        if match is None or match.group(1) != character:
            raise UnreadableGeometryError(f"expected {character!r}")
        self.pos = match.end()
        if character == "(":
            self._open()
        elif character == ")":
            self.depth -= 1

    def _open(self) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise UnreadableGeometryError(f"the geometry nests more than {MAX_NESTING} deep")

    def _header(self, base: int) -> None:
        self.out.write(struct.pack("<BI", 1, base + 1000 * self.z + 2000 * self.m))

    def _items(self, read_one: Callable[[], None]) -> int:
        """Read ``( item, item, ... )`` into a count placeholder, returning how many there were."""
        at = self.out.tell()
        self.out.write(b"\0\0\0\0")
        self._expect("(")
        count = 0
        while True:
            read_one()
            count += 1
            if self._punctuation() != ",":
                break
            self._expect(",")
        self._expect(")")
        self.out.seek(at)
        self.out.write(struct.pack("<I", count))
        self.out.seek(0, io.SEEK_END)
        return count

    def _body(self, base: int, members_required: tuple[bool, bool] | None = None) -> None:
        """A geometry of type *base*, its keyword and tag already read, written with its header."""
        self._header(base)
        empty = self._empty()
        if base == 1:
            if empty:
                self.out.write(struct.pack(f"<{self.ordinates}d", *[float("nan")] * self.ordinates))
            else:
                self._sequence(expect_one=True)
        elif empty:
            self.out.write(b"\0\0\0\0")
        elif base == 2:
            self._sequence()
        elif base == 3:
            self._items(self._ring)
        elif base == 4:
            self._multipoint()
        elif base in (5, 6):
            self._items(lambda: self._body(base - 3))
        else:
            self._items(lambda: self._geometry(required=members_required))

    def _ring(self) -> None:
        if self._empty():
            self.out.write(b"\0\0\0\0")
        else:
            self._sequence()

    def _multipoint(self) -> None:
        """GEOS reads a multipoint's members as its first one is written: each ``(x y)`` or ``EMPTY``, or all bare."""
        parenthesized = _PARENTHESIZED_MEMBERS.match(self.text, self.pos) is not None
        if not parenthesized and self.ordinates == 4 and not self.tagged:
            # GEOS reads these as XYZ and drops each fourth number.
            raise UnreadableGeometryError("an untagged multipoint of bare four-number points")
        self._items(lambda: self._body(1) if parenthesized else self._bare_point())

    def _bare_point(self) -> None:
        self._header(1)
        match = _BARE_POINT.match(self.text, self.pos)
        end = match.end() if match else self.pos
        self._parse(self.pos, end)
        self.pos = end

    def _sequence(self, *, expect_one: bool = False) -> None:
        """``( x y, x y, ... )``: its point count, unless *expect_one*, then its ordinates."""
        self._expect("(")
        end = self.text.find(")", self.pos)
        if end < 0 or self.text.find("(", self.pos, end) >= 0:
            raise UnreadableGeometryError("expected coordinates")
        count = self.text.count(",", self.pos, end) + 1
        if expect_one:
            if count != 1:
                raise UnreadableGeometryError("a point has one coordinate")
        else:
            self.out.write(struct.pack("<I", count))
        self._parse(self.pos, end)
        self.pos = end + 1
        self.depth -= 1

    def _parse(self, start: int, end: int) -> None:
        """Write the coordinates in ``text[start:end]``, a chunk of whole coordinates at a time."""
        pattern = _COORDINATES[self.ordinates]
        while True:
            stop = min(end, start + _CHUNK_CHARACTERS)
            if stop < end:
                comma = self.text.find(",", stop, end)
                stop = end if comma < 0 else comma
            chunk = self.text[start:stop]
            if pattern.fullmatch(chunk) is None:
                raise UnreadableGeometryError("expected coordinates")
            values = np.array(chunk.replace(",", " ").split(), dtype="<f8")
            if not np.isfinite(values).all():
                raise UnreadableGeometryError("a coordinate is not a finite number")
            self.out.write(values.tobytes())
            if stop >= end:
                return
            start = stop + 1
