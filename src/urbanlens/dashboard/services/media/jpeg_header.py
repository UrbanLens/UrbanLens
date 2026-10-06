"""A JPEG's pixel size, read from its frame header. Nothing here decodes an image.

Keywording runs on an ordinary, credentialed worker, which must never decode an upload (``docs/MEDIA_PIPELINE.md``):
the risk there is memory corruption inside a native decoder. Reading a size needs no decoder. :func:`jpeg_dimensions`
walks the marker segments before the first frame header (ITU-T T.81, Annex B) and returns the width and height that
header declares: bounds-checked slicing of a Python ``bytes`` that stops before any image data, with no native code
under it.

It is used only on the analysis copy, which the sandbox worker wrote by re-encoding the upload with Pillow
(:func:`~urbanlens.dashboard.services.media.images.write_image_analysis_thumbnail`), never on bytes a user uploaded.
Anything it cannot read is None, left to whoever asked.
"""

from __future__ import annotations

_SOI = b"\xff\xd8"
_MARKER_PREFIX = 0xFF
#: Every start-of-frame marker (SOF0-SOF15) except DHT (C4), JPG (C8) and DAC (CC), which share the range.
_START_OF_FRAME = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})
#: Markers with no length field after them: TEM, and the restart markers RST0-RST7.
_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})
#: Markers that cannot come before the first frame header: a stuffed zero (not a marker), SOI, EOI, and SOS - the
#: first scan, whose frame header must already have been seen.
_NOT_BEFORE_A_FRAME = frozenset({0x00, 0xD8, 0xD9, 0xDA})
#: A frame header's fixed fields, counting its own length: Lf (2), P (1), Y (2), X (2), Nf (1).
_FRAME_HEADER_FIXED_BYTES = 8


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """The ``(width, height)`` a JPEG's first frame header declares, without decoding the image.

    Args:
        data: The file's bytes. Only the segments before the first frame header are read.

    Returns:
        ``(width, height)`` in pixels; None when ``data`` is not a JPEG, or ends or goes wrong before a whole frame
        header, or the frame leaves its height to a later DNL marker (height 0) or declares a width of 0.
    """
    if not data.startswith(_SOI):
        return None
    end = len(data)
    position = len(_SOI)
    # Every pass moves ``position`` forward by at least one byte, so this ends within len(data) passes.
    while position < end:
        if data[position] != _MARKER_PREFIX:
            return None
        while position < end and data[position] == _MARKER_PREFIX:
            position += 1  # a marker may be preceded by any number of 0xFF fill bytes
        if position >= end:
            return None
        marker = data[position]
        position += 1
        if marker in _STANDALONE:
            continue
        if marker in _NOT_BEFORE_A_FRAME or position + 2 > end:
            return None
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > end:
            return None
        if marker in _START_OF_FRAME:
            if length < _FRAME_HEADER_FIXED_BYTES:
                return None
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            return (width, height) if width and height else None
        position += length
    return None
