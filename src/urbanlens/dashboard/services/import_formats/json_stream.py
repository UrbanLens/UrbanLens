"""Reading a JSON import a value at a time, so the document is never built whole.

``json.loads`` on a Takeout export holds Python objects several times the file's size, and an import preview entry
may be up to 1 GB. ijson's C backend reads the file in chunks and builds one array item at a time.
"""

from __future__ import annotations

import itertools
from typing import IO, TYPE_CHECKING, Any

import ijson

from urbanlens.dashboard.services.import_formats.streams import skip_bom_and_whitespace
from urbanlens.dashboard.services.sandbox import untrusted_parse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_VALUE_KINDS = {"start_array": "array", "start_map": "object", "integer": "number", "double": "number"}
# yajl refuses an integer wider than 64 bits, which json.loads reads; ijson's pure-Python parser reads it, slowly.
_WIDE_INTEGER_BACKEND = ijson.get_backend("python")


class MalformedJSONError(ValueError):
    """A JSON file is not well-formed, or not UTF-8: a ``ValueError``, as ``json.loads`` raises."""


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
        events = _read(stream, lambda backend: backend.parse(stream))
        _, first, _ = next(events)
        if first != "start_map":
            return None
        return {value for prefix, event, value in events if event == "map_key" and not prefix}
    except (ijson.JSONError, UnicodeDecodeError):
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
        events = _read(stream, lambda backend: backend.parse(stream))
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
        yield from _read(stream, lambda backend: backend.items(stream, f"{key}.item", use_float=True))
    except (ijson.JSONError, UnicodeDecodeError) as exc:
        raise MalformedJSONError(str(exc)) from exc


def _read[T](stream: IO[bytes], read: Callable[[Any], Iterator[T]]) -> Iterator[T]:
    """What *read* makes of *stream* through yajl, or through ijson's own parser once yajl meets a wide integer.

    The re-read starts where the first did and skips what the first already produced.
    """
    start = stream.tell()
    produced = 0
    try:
        for value in read(ijson):
            yield value
            produced += 1
        return
    except ijson.JSONError as exc:
        if "integer overflow" not in str(exc):
            raise
    stream.seek(start)
    yield from itertools.islice(read(_WIDE_INTEGER_BACKEND), produced, None)
