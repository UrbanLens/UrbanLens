"""Remove metadata segments from an upload's bytes without decoding the image.

Currently unused by the live pipeline - see below - but kept as a working,
tested utility rather than deleted, since a future in-request use has nowhere
else to reach for a decode-free strip.

``downscale_stored_image`` also drops metadata, but it does so by decoding and
re-encoding the whole image, which is far too expensive to run in a request:
under gunicorn's gevent worker a decode does not yield, so it stalls every other
request on that worker. That is why it runs on Celery. This module offered a
different way to close that window: container formats keep their metadata in
discrete, length-prefixed segments, so the segments can be dropped by walking
the byte stream - no pixel data touched, the cost a copy rather than a decode -
which used to make it safe to run inside the request, before storage ever saw
the bytes.

It no longer runs there. Reading metadata *before* stripping it was one
operation for a reason - stripping first loses ``exif_data``/GPS/``taken_at``
the app otherwise keeps - and the read half (the ``extract_*`` functions in
``services.media.images``) is a Pillow decode, which is exactly the class of
code the sandbox tier (``services.sandbox.guard``) exists to keep out of a
request process. ``Image.pending_scan`` (see ``prepare_photo_upload``) now
closes the same "raw file briefly servable" window through access control
instead: the file sits there unstripped for as long as the sandboxed task
needs, but nobody besides the uploader may read or list it until then - which
this module's byte-walk approach could never offer for the formats it
doesn't handle (HEIC, TIFF, GIF, ...) anyway.

Formats it cannot handle return ``None`` rather than a guess.
"""

from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

#: JPEG APP markers carrying metadata rather than data the decoder needs.
#: APP1 is EXIF and XMP, APP13 is Photoshop/IPTC. APP0 (JFIF) and APP2
#: (ICC colour profile) are deliberately kept - dropping APP2 shifts the
#: colours of a wide-gamut photo.
_JPEG_DROP_MARKERS = frozenset({0xE1, 0xED})
_JPEG_COMMENT_MARKER = 0xFE
#: Start of Scan: everything after it is entropy-coded pixel data.
_JPEG_SOS = 0xDA
_JPEG_EOI = 0xD9

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: Textual and EXIF chunks. PNG chunks are self-delimiting and independently
#: CRC'd, so dropping one whole chunk leaves the rest valid as they stand.
_PNG_DROP_CHUNKS = frozenset({b"tEXt", b"zTXt", b"iTXt", b"eXIf"})

_WEBP_DROP_CHUNKS = frozenset({b"EXIF", b"XMP "})
#: Bit flags in a VP8X chunk announcing that EXIF / XMP chunks follow. Left set
#: after the chunks are gone, a strict decoder is entitled to go looking.
_VP8X_EXIF_FLAG = 0x08
_VP8X_XMP_FLAG = 0x04


def strip_metadata(data: bytes) -> bytes | None:
    """Return *data* with metadata segments removed.

    Args:
        data: The complete uploaded file.

    Returns:
        The stripped bytes, or ``None`` when the format is not one this module
        rewrites (HEIC, TIFF, GIF, anything unrecognised) or the stream does not
        parse. ``None`` means "leave it to the re-encode", never "it was clean".
    """
    try:
        if data[:2] == b"\xff\xd8":
            return _strip_jpeg(data)
        if data[:8] == _PNG_SIGNATURE:
            return _strip_png(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _strip_webp(data)
    except (IndexError, struct.error, ValueError) as exc:
        # A malformed upload is not worth an exception here: the caller stores
        # it as-is and the Celery re-encode deals with it (or rejects it).
        logger.warning("Metadata strip failed, leaving bytes untouched: %s", exc)
        return None
    return None


def _strip_jpeg(data: bytes) -> bytes | None:
    """Drop EXIF/XMP/IPTC/comment segments from a JPEG."""
    out = bytearray(data[:2])
    i = 2
    end = len(data)
    while i < end:
        if data[i] != 0xFF:
            return None
        # Fill bytes: any number of 0xFF may pad the gap before a marker.
        marker_at = i
        while marker_at < end and data[marker_at] == 0xFF:
            marker_at += 1
        if marker_at >= end:
            return None
        marker = data[marker_at]
        if marker in (_JPEG_SOS, _JPEG_EOI):
            # Entropy-coded data from here on; copy the remainder verbatim.
            out += data[i:]
            return bytes(out)
        segment_start = marker_at + 1
        if segment_start + 2 > end:
            return None
        (length,) = struct.unpack(">H", data[segment_start : segment_start + 2])
        if length < 2:
            return None
        segment_end = segment_start + length
        if segment_end > end:
            return None
        drop = marker in _JPEG_DROP_MARKERS or marker == _JPEG_COMMENT_MARKER
        if not drop:
            out += data[i:segment_end]
        i = segment_end
    return bytes(out)


def _strip_png(data: bytes) -> bytes | None:
    """Drop text and EXIF chunks from a PNG."""
    out = bytearray(_PNG_SIGNATURE)
    i = len(_PNG_SIGNATURE)
    end = len(data)
    while i + 8 <= end:
        (length,) = struct.unpack(">I", data[i : i + 4])
        chunk_type = data[i + 4 : i + 8]
        chunk_end = i + 12 + length  # length + type + data + crc
        if chunk_end > end:
            return None
        if chunk_type not in _PNG_DROP_CHUNKS:
            out += data[i:chunk_end]
        i = chunk_end
        if chunk_type == b"IEND":
            break
    return bytes(out)


def _strip_webp(data: bytes) -> bytes | None:
    """Drop EXIF/XMP chunks from a RIFF/WebP container and clear their flags."""
    out = bytearray()
    i = 12
    end = len(data)
    while i + 8 <= end:
        fourcc = data[i : i + 4]
        (size,) = struct.unpack("<I", data[i + 4 : i + 8])
        payload_end = i + 8 + size
        if payload_end > end:
            return None
        chunk_end = payload_end + (size & 1)  # chunks are padded to even length
        if fourcc not in _WEBP_DROP_CHUNKS:
            chunk = bytearray(data[i : min(chunk_end, end)])
            if fourcc == b"VP8X" and len(chunk) >= 9:
                chunk[8] &= ~(_VP8X_EXIF_FLAG | _VP8X_XMP_FLAG) & 0xFF
            out += chunk
        i = chunk_end
    # RIFF size counts everything after the size field itself.
    return b"RIFF" + struct.pack("<I", len(out) + 4) + b"WEBP" + bytes(out)
