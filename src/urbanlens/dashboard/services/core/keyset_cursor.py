"""Opaque ``(timestamp, pk)`` cursors for keyset-paginated feeds.

Keyset pagination is what every browse/sync feed on the external API uses
instead of page numbers: concurrent inserts reorder page-numbered results
under the reader, silently dropping or duplicating rows. A cursor pins the
exact position instead.

The token is base64 of ``"<isoformat>|<pk>"`` - opaque to clients (so the
shape stays free to change) but not secret, since it only encodes a position
in a feed the caller is already authorized to read.

``services.pins.pin_sync`` predates this module and still carries its own private
copies of the same two functions; it was left alone deliberately rather than
refactored underneath a well-tested sync path.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime

from django.utils import timezone


class InvalidCursorError(ValueError):
    """The supplied cursor is malformed or was never issued by this service.

    ``safe_message`` is safe to surface to the caller.
    """

    def __init__(self, message: str = "Invalid cursor.") -> None:
        """Initialize with a caller-safe message.

        Args:
            message: Human-readable detail to surface.
        """
        self.safe_message = message
        super().__init__(message)


def encode_cursor(stamp: datetime, pk: int) -> str:
    """Encode a ``(timestamp, pk)`` keyset position as a URL-safe token.

    Args:
        stamp: The row's ordering timestamp.
        pk: The row's primary key, which breaks ties between rows sharing a
            timestamp - without it a batch of simultaneously-created rows
            would page inconsistently.

    Returns:
        A URL-safe base64 token.
    """
    return base64.urlsafe_b64encode(f"{stamp.isoformat()}|{pk}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, int]:
    """Decode a token back into its keyset position.

    Args:
        cursor: A token previously produced by :func:`encode_cursor`.

    Returns:
        The ``(timestamp, pk)`` position the next page continues from.

    Raises:
        InvalidCursorError: The token is malformed, or decodes to a naive
            datetime (every stored timestamp here is timezone-aware, so a
            naive one means the token was not ours).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp_raw, _, pk_raw = raw.rpartition("|")
        stamp = datetime.fromisoformat(stamp_raw)
        pk = int(pk_raw)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError from exc
    if timezone.is_naive(stamp):
        raise InvalidCursorError
    return stamp, pk
