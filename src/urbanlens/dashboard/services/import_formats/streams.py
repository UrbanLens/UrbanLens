"""Reading an uploaded file a piece at a time, so that how large it is costs disk rather than memory.

An import preview entry may be as large as ``archive_extractor._MAX_SINGLE_FILE_BYTES``. The helpers here let a
parser read one from a seekable binary file in fixed-size chunks.
"""

from __future__ import annotations

import codecs
import io
import re
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

#: How much of a file one read takes.
READ_CHUNK_BYTES = 256 * 1024

_UTF8_BOM = codecs.BOM_UTF8

# Every boundary str.splitlines() splits on.
_LINE_BREAK_RE = re.compile("[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")


def as_stream(data: bytes | IO[bytes]) -> IO[bytes]:
    """A binary file over *data*, so parsers take bytes and files alike.

    Args:
        data: The file's bytes, or a seekable binary file positioned at its start.

    Returns:
        A seekable binary file.
    """
    return io.BytesIO(data) if isinstance(data, bytes | bytearray) else data


def iter_decoded(stream: IO[bytes], encoding: str) -> Iterator[str]:
    """Decode *stream* a chunk at a time.

    Args:
        stream: The file, read from its current position.
        encoding: A codec name, e.g. ``"utf-8-sig"``.

    Yields:
        Successive pieces of the text; together, what ``stream.read().decode(encoding)`` returns.

    Raises:
        UnicodeDecodeError: The file is not valid in *encoding*.
    """
    decoder = codecs.getincrementaldecoder(encoding)()
    while chunk := stream.read(READ_CHUNK_BYTES):
        if text := decoder.decode(chunk):
            yield text
    if tail := decoder.decode(b"", final=True):
        yield tail


class LineTooLongError(ValueError):
    """A line ran past the length its reader allows."""


def iter_lines(pieces: Iterable[str], *, max_line: int | None = None) -> Iterator[str]:
    """The lines of a text that arrives in pieces, split exactly as ``str.splitlines`` splits.

    Only a line is held whole, never the text.

    Args:
        pieces: Successive pieces of the text.
        max_line: The longest line to hold, in characters; None for no bound.

    Yields:
        Each line, without its line break.

    Raises:
        LineTooLongError: A line ran past ``max_line``, raised once it does rather than once it ends.
    """
    held: list[str] = []
    held_length = 0
    for piece in pieces:
        held.append(piece)
        held_length += len(piece)
        if not _LINE_BREAK_RE.search(piece):
            if max_line is not None and held_length > max_line:
                raise LineTooLongError(f"a line is longer than {max_line:,} characters")
            continue
        lines = "".join(held).splitlines(keepends=True)
        # The last line may go on in the next piece, as may a "\r" that the next piece's "\n" completes.
        held = [lines.pop()]
        held_length = len(held[0])
        for line in lines:
            if max_line is not None and len(line) > max_line + 2:
                raise LineTooLongError(f"a line is longer than {max_line:,} characters")
            yield line.splitlines()[0]
    remainder = "".join(held)
    if max_line is not None and len(remainder) > max_line:
        raise LineTooLongError(f"a line is longer than {max_line:,} characters")
    yield from remainder.splitlines()


def is_valid_text(stream: IO[bytes], encoding: str) -> bool:
    """Whether the rest of *stream* decodes in *encoding*, read a chunk at a time.

    Args:
        stream: The file, read from its current position to its end.
        encoding: A codec name.

    Returns:
        True when every byte decodes.
    """
    try:
        for _ in iter_decoded(stream, encoding):
            pass
    except UnicodeDecodeError:
        return False
    return True


def skip_bom_and_whitespace(stream: IO[bytes], limit: int = READ_CHUNK_BYTES) -> str:
    """Move *stream* past a UTF-8 byte-order mark and the whitespace ``str.lstrip`` would remove.

    Args:
        stream: A seekable file positioned at its start.
        limit: The most text to return.

    Returns:
        Up to *limit* characters of the text that follows, the stream left positioned at its first byte;
        empty when nothing but whitespace follows.

    Raises:
        UnicodeDecodeError: What precedes the text, or the text returned, is not UTF-8.
    """
    start = stream.tell()
    skipped = len(_UTF8_BOM) if stream.read(len(_UTF8_BOM)) == _UTF8_BOM else 0
    stream.seek(start + skipped)
    decoder = codecs.getincrementaldecoder("utf-8")()
    head = ""
    while len(head) < limit and (chunk := stream.read(READ_CHUNK_BYTES)):
        text = decoder.decode(chunk)
        if not head:
            stripped = text.lstrip()
            skipped += len(text[: len(text) - len(stripped)].encode())
            text = stripped
        head += text
    stream.seek(start + skipped)
    return head[:limit]
