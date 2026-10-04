"""Reading a JSON import a value at a time, so the document is never built whole.

``json.loads`` on a Takeout export holds Python objects several times the file's size, and an import preview entry
may be up to 1 GB. ijson's C backend reads the file in chunks and builds one array item at a time.
"""

from __future__ import annotations

from array import array
import itertools
import re
from typing import IO, TYPE_CHECKING, Any

import ijson
import numpy as np

from urbanlens.dashboard.services.import_formats.streams import skip_bom_and_whitespace
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_VALUE_KINDS = {"start_array": "array", "start_map": "object", "integer": "number", "double": "number"}
# yajl refuses an integer outside the signed 64-bit range, which json.loads reads; ijson's pure-Python parser reads it, slowly.
_WIDE_INTEGER_BACKEND = ijson.get_backend("python")
#: Where a GeoJSON file's geometries sit, a GeometryCollection's members included.
_GEOMETRY_PREFIX = re.compile(r"features\.item\.geometry(?:\.geometries\.item)*")
_CONTAINER_STARTS = frozenset({"start_map", "start_array"})
_CONTAINER_ENDS = frozenset({"end_map", "end_array"})


class MalformedJSONError(ValueError):
    """A JSON file is not well-formed, or not UTF-8: a ``ValueError``, as ``json.loads`` raises."""


class _StrictWhitespace:
    """A JSON file whose reads refuse a vertical tab or form feed, which yajl reads as whitespace and JSON does not.

    Neither byte can be part of a UTF-8 sequence, or stand unescaped inside a JSON string.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if b"\x0b" in data or b"\x0c" in data:
            raise MalformedJSONError("A vertical tab or form feed is not JSON whitespace.")
        return data


@untrusted_parse("json.stream")
def top_level_keys(stream: IO[bytes]) -> set[str] | None:
    """The keys of the object a JSON file holds, reading the whole file but building none of it.

    Args:
        stream: A seekable file positioned at its start. A byte-order mark and leading whitespace are skipped.

    Returns:
        The top-level keys, or None when the file is not one well-formed JSON object.
    """
    try:
        skip_bom_and_whitespace(stream, limit=1)
        events = _read(stream, lambda backend, source: backend.parse(source))
        _, first, _ = next(events)
        if first != "start_map":
            return None
        return {value for prefix, event, value in events if event == "map_key" and not prefix}
    except (ijson.JSONError, UnicodeDecodeError, MalformedJSONError):
        return None


@untrusted_parse("json.stream")
def top_level_value_kind(stream: IO[bytes], key: str) -> str | None:
    """What a top-level JSON object holds under *key*, reading only as far as the key.

    Args:
        stream: A seekable file positioned at its start. A byte-order mark and leading whitespace are skipped.
        key: The top-level key.

    Returns:
        ``"array"``, ``"object"``, or the scalar's kind (``"string"``, ``"number"``, ``"boolean"``, ``"null"``);
        None when the file holds no object with that key.

    Raises:
        MalformedJSONError: The file is not well-formed JSON up to the key.
    """
    try:
        skip_bom_and_whitespace(stream, limit=1)
        events = _read(stream, lambda backend, source: backend.parse(source))
        _, first, _ = next(events, (None, None, None))
        if first != "start_map":
            return None
        for prefix, event, value in events:
            if event == "map_key" and not prefix and value == key:
                _, kind, _ = next(events)  # ijson raises rather than ending after a key
                return _VALUE_KINDS.get(kind, kind)
    except (ijson.JSONError, UnicodeDecodeError) as exc:
        raise MalformedJSONError(str(exc)) from exc
    return None


@untrusted_parse("json.stream")
def iter_array_items(stream: IO[bytes], key: str) -> Iterator[Any]:
    """Each item of the array a top-level JSON object holds under *key*, built one at a time.

    Numbers come back as ``int`` and ``float``, as ``json.loads`` returns them.

    Args:
        stream: A seekable file positioned at its start. A byte-order mark and leading whitespace are skipped.
        key: The top-level key.

    Yields:
        Each item; nothing when there is no such key or its value is not an array.

    Raises:
        MalformedJSONError: The file is not well-formed JSON, up to the item where reading stopped.
    """
    try:
        skip_bom_and_whitespace(stream, limit=1)
        yield from _read(stream, lambda backend, source: backend.items(source, f"{key}.item", use_float=True))
    except (ijson.JSONError, UnicodeDecodeError) as exc:
        raise MalformedJSONError(str(exc)) from exc


@untrusted_parse("json.stream")
def iter_geojson_features(stream: IO[bytes]) -> Iterator[Any]:
    """Each item of a GeoJSON file's ``features`` array, as :func:`iter_array_items` builds it, but compact.

    A geometry's array of positions - every item a list of two or three numbers - arrives as one ``float64`` array of
    shape ``(n, 2)`` or ``(n, 3)``, where a list of lists costs about seven times its text. Any other array under
    ``coordinates`` is built as ``json.loads`` builds it. The rest of the feature is built from ijson's events in
    Python, which is slower than its C builder; the preview's pin cap bounds how many features that is.

    Args:
        stream: A seekable file positioned at its start. A byte-order mark and leading whitespace are skipped.

    Yields:
        Each feature; nothing when there is no ``features`` array.

    Raises:
        MalformedJSONError: The file is not well-formed JSON, up to the feature where reading stopped.
    """
    try:
        skip_bom_and_whitespace(stream, limit=1)
        yield from _read(stream, lambda backend, source: _features(backend.parse(source, use_float=True)))
    except (ijson.JSONError, UnicodeDecodeError) as exc:
        raise MalformedJSONError(str(exc)) from exc


def _features(events: Iterator[tuple[str, str, Any]]) -> Iterator[Any]:
    for prefix, event, value in events:
        if prefix != "features.item" or event in _CONTAINER_ENDS:
            continue
        yield _built(event, events) if event in _CONTAINER_STARTS else value


def _built(first: str, events: Iterator[tuple[str, str, Any]]) -> Any:
    """The container whose start event was *first*, consuming *events* through its end."""
    root: Any = {} if first == "start_map" else []
    containers: list[Any] = [root]
    keys: list[Any] = [None]
    for prefix, event, value in events:
        if event == "map_key":
            if value == "coordinates" and _GEOMETRY_PREFIX.fullmatch(prefix):
                containers[-1][value] = _coordinates(events)
            else:
                keys[-1] = value
            continue
        if event in _CONTAINER_ENDS:
            containers.pop()
            keys.pop()
            if not containers:
                return root
            continue
        item: Any = {} if event == "start_map" else [] if event == "start_array" else value
        parent = containers[-1]
        if type(parent) is dict:
            parent[keys[-1]] = item
        else:
            parent.append(item)
        if event in _CONTAINER_STARTS:
            containers.append(item)
            keys.append(None)
    raise MalformedJSONError("The file ended inside a feature.")


def _coordinates(events: Iterator[tuple[str, str, Any]]) -> Any:
    """The value under a geometry's ``coordinates`` key, with each array of positions held as one float array."""
    _, first, value = next(events, (None, None, None))
    if first is None:
        raise MalformedJSONError("The file ended inside a geometry.")
    if first == "start_map":
        return _built(first, events)
    if first != "start_array":
        return value
    arrays = [_PositionArray()]
    for _, event, item in events:
        if event == "start_array":
            arrays.append(_PositionArray())
        elif event == "end_array":
            done = arrays.pop().value()
            if not arrays:
                return done
            arrays[-1].add(done)
        elif event == "start_map":
            arrays[-1].add(_built(event, events))
        else:
            arrays[-1].add(item)
    raise MalformedJSONError("The file ended inside a geometry.")


def _is_position(item: Any) -> bool:
    """Whether *item* is a list of two or three numbers that a ``float64`` holds exactly as ``json.loads`` read them."""
    return type(item) is list and 2 <= len(item) <= 3 and all(type(number) is float or (type(number) is int and -(2**53) <= number <= 2**53) for number in item)


class _PositionArray:
    """A JSON array, held as one float array for as long as every item is a position of the first one's size."""

    __slots__ = ("flat", "items", "size")

    def __init__(self) -> None:
        self.flat: array[float] | None = None
        self.size = 0
        self.items: list[Any] | None = None

    def add(self, item: Any) -> None:
        if self.items is None and _is_position(item) and self.size in {0, len(item)}:
            if self.flat is None:
                self.flat, self.size = array("d"), len(item)
            self.flat.extend(item)
            return
        if self.items is None:
            self.items = self._as_lists()
        self.items.append(item)

    def value(self) -> Any:
        if self.items is not None:
            return self.items
        if self.flat is None:
            return []
        return np.frombuffer(self.flat, dtype=np.float64).reshape(-1, self.size)

    def _as_lists(self) -> list[Any]:
        if self.flat is None:
            return []
        return [self.flat[start : start + self.size].tolist() for start in range(0, len(self.flat), self.size)]


def _read[T](stream: IO[bytes], read: Callable[[Any, _StrictWhitespace], Iterator[T]]) -> Iterator[T]:
    """What *read* makes of *stream* through yajl, or through ijson's own parser once yajl meets a wide integer.

    The re-read starts where the first did and skips what the first already produced.
    """
    start = stream.tell()
    produced = 0
    try:
        for value in read(ijson, _StrictWhitespace(stream)):
            yield value
            produced += 1
        return
    except ijson.JSONError as exc:
        if "integer overflow" not in str(exc):
            raise
    stream.seek(start)
    yield from itertools.islice(read(_WIDE_INTEGER_BACKEND, _StrictWhitespace(stream)), produced, None)
