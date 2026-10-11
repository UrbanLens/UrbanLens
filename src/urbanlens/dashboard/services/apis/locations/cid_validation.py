"""Checks a Google Maps CID must pass before anything is sent, stored or asked about it.

A CID is an unsigned 64-bit integer. Above 2**53 a float64 cannot hold one exactly, and a CID that
went through one - a JavaScript ``Number``, ``float()``, ``Decimal(str(float))`` - comes back as the
float's shortest round-trip digits padded with zeros. REData stored 1,945 such ids on 2026-07-31
(REData's P120) and asked Google about every one of them, nightly, for months.

The rules match REData's ``google_places.cid_validation``, which applies them to every entry of
``POST /places/resolve-cids/`` and reports a refusal with the same :class:`InvalidCidError` codes.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
from typing import Final

MAX_CID: Final = 0xFFFF_FFFF_FFFF_FFFF
#: Every integer up to this survives a float64 exactly, so nothing at or below it can be float-rounded.
FLOAT64_EXACT_LIMIT: Final = 2**53

CID_NOT_INTEGER: Final = "cid_not_integer"
CID_OUT_OF_RANGE: Final = "cid_out_of_range"
CID_FLOAT_ROUNDED: Final = "cid_float_rounded"
CID_CONFLICTS_WITH_URL: Final = "cid_conflicts_with_url"

_DIGITS = re.compile(r"[0-9]{1,20}")
_FEATURE_ID = re.compile(r"0x[0-9a-f]+:0x([0-9a-f]+)", re.IGNORECASE)
_URL_FEATURE_ID = re.compile(r"!1s0x[0-9a-f]+:0x([0-9a-f]+)", re.IGNORECASE)
_URL_CID = re.compile(r"[?&]cid=([0-9]{1,20})(?![0-9])")


class InvalidCidError(ValueError):
    """A value that cannot be a Google Maps CID, or one that has visibly lost precision.

    Attributes:
        code: One of the ``CID_*`` codes in this module, as REData reports it.
        message: Why, for a log line or an API error body.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_cid(value: object) -> int:
    """The CID *value* states, refusing anything that is not exactly an unsigned 64-bit integer.

    Args:
        value: An ``int``, an integral ``Decimal`` (a ``DecimalField`` read), or a string of decimal digits.
            A ``float`` or ``bool`` is refused outright - a float has already lost the digits that matter.

    Returns:
        The CID.

    Raises:
        InvalidCidError: ``cid_not_integer`` or ``cid_out_of_range``.
    """
    if isinstance(value, bool) or not isinstance(value, int | str | Decimal):
        raise InvalidCidError(CID_NOT_INTEGER, f"A CID must be an integer or a string of digits, not {type(value).__name__}.")
    if isinstance(value, str):
        stripped = value.strip()
        if not _DIGITS.fullmatch(stripped):
            raise InvalidCidError(CID_NOT_INTEGER, "A CID string must be 1 to 20 decimal digits.")
        cid = int(stripped)
    elif isinstance(value, Decimal):
        try:
            integral = value.is_finite() and value == value.to_integral_value()
        except InvalidOperation:
            integral = False
        if not integral:
            raise InvalidCidError(CID_NOT_INTEGER, "A CID must be a whole number.")
        cid = int(value)
    else:
        cid = value
    if not 0 < cid <= MAX_CID:
        raise InvalidCidError(CID_OUT_OF_RANGE, "A CID must be between 1 and 2**64 - 1.")
    return cid


def float_rounded(cid: int) -> int:
    """What *cid* becomes after a trip through a float64 and back to decimal digits."""
    return int(Decimal(repr(float(cid))))


def looks_float_rounded(cid: int) -> bool:
    """Whether *cid* is exactly what a float64 prints for itself, which a real CID rarely is.

    Above 2**53 only about one 64-bit integer in several hundred has this shape, and every CID that
    went through a JavaScript ``Number`` has it. Never conclusive on its own: refuse such a CID only
    when nothing corroborates it.
    """
    return cid > FLOAT64_EXACT_LIMIT and float_rounded(cid) == cid


def cid_stated_by(url: str) -> int | None:
    """The CID a Google Maps URL or feature id carries, if it carries one.

    Args:
        url: A place URL with a ``!1s0x...:0x<cid>`` data segment, a ``?cid=<digits>`` URL, or a bare
            ``0x...:0x<cid>`` feature id.

    Returns:
        The CID, or None when there is none - including a feature id whose CID half is zero, which
        names a place by its S2 cell alone.
    """
    if not url:
        return None
    text = url.strip()
    match = _URL_FEATURE_ID.search(text) or _FEATURE_ID.fullmatch(text)
    if match:
        cid = int(match.group(1), 16)
    else:
        match = _URL_CID.search(text)
        if match is None:
            return None
        cid = int(match.group(1))
    return cid if 0 < cid <= MAX_CID else None


def reconcile_cid(claimed: object, *, url: str = "") -> int | None:
    """The one CID an import entry names, from the CID it claims and the URL it came from.

    A URL that states a CID is authoritative: it is Google's own value, written by Google. A claimed
    CID that is that value after a float64 round trip is a copy that lost its low digits, and the
    URL's value replaces it.

    Args:
        claimed: The entry's own ``cid``, or None.
        url: The entry's source Google Maps URL, if any.

    Returns:
        The CID, or None when the entry names none at all.

    Raises:
        InvalidCidError: ``cid_not_integer``/``cid_out_of_range`` for a malformed claim,
            ``cid_conflicts_with_url`` when the claim and the URL name different places, or
            ``cid_float_rounded`` when only a float-shaped claim is available to go on.
    """
    cid = None if claimed is None or claimed == "" else parse_cid(claimed)
    stated = cid_stated_by(url)
    if stated is not None:
        if cid is None or cid == stated:
            return stated
        if float_rounded(stated) == cid:
            return stated
        raise InvalidCidError(CID_CONFLICTS_WITH_URL, f"The CID {cid} disagrees with the CID {stated} in its own Google Maps URL.")
    if cid is not None and looks_float_rounded(cid):
        raise InvalidCidError(
            CID_FLOAT_ROUNDED,
            f"The CID {cid} is exactly what a float64 prints for itself, the mark of a CID that lost its low digits to a float; send the place's Google Maps URL or feature id instead.",
        )
    return cid
