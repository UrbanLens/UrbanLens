"""Content-derived identities for imported pins, so a re-import recognises a pin wherever the user has since moved it."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import re

from urbanlens.dashboard.models.location.queryset import quantize_coordinate

_WHITESPACE = re.compile(r"\s+")


def takeout_import_key(*, cid: int | str | Decimal | None, place_id: str | None, name: str | None, latitude: float | Decimal, longitude: float | Decimal) -> str:
    """
    The batch-independent key of one Google Takeout row.
    The Google cid when the row has one, else its place_id, else a digest of the normalised name and the point at 6 dp. A digest keeps the key short and keeps coordinates out of the column.

    Args:
        cid: Google Maps CID, if the row carries one.
        place_id: Google place id, if the row carries one.
        name: The row's title.
        latitude: Row latitude, used only when there is no cid or place_id.
        longitude: Row longitude, used only when there is no cid or place_id.
    """
    if cid not in (None, ""):
        try:
            return f"cid:{int(Decimal(str(cid)))}"
        except (InvalidOperation, ValueError, OverflowError):
            # A malformed cid is no identity; fall back to the place id or the name-and-point digest.
            pass
    if place_id:
        return f"place:{place_id}"
    normalised = _WHITESPACE.sub(" ", (name or "").casefold()).strip()
    point = f"{quantize_coordinate(latitude, 'latitude')},{quantize_coordinate(longitude, 'longitude')}"
    return "ll:" + hashlib.sha256(f"{normalised}|{point}".encode()).hexdigest()
