"""Normalize wireless-device MAC addresses to one canonical storage form.

Every write path (the upload serializer, ``ScannedDevice.objects.get_or_create_for_mac``)
must funnel through :func:`normalize_mac_address` - otherwise the same physical
device submitted as ``aa:bb:cc:dd:ee:ff`` by one upload and ``AA-BB-CC-DD-EE-FF``
by another would silently create two ``ScannedDevice`` rows.
"""

from __future__ import annotations

import re

#: Accepts colon-, hyphen-, or dot-separated hex octets, or none at all -
#: the separator styles actually seen across mobile Bluetooth/WiFi scan APIs.
_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:\-.]?[0-9A-Fa-f]{2}){5}$")


class InvalidMacAddressError(ValueError):
    """Raised when a string cannot be parsed as a 6-octet MAC address.

    ``safe_message`` is safe to surface directly to the caller - it only ever
    echoes back the value the caller itself submitted.
    """

    def __init__(self, message: str) -> None:
        self.safe_message = message
        super().__init__(message)


def normalize_mac_address(raw_mac_address: str) -> str:
    """Return *raw_mac_address* in canonical upper-case colon-separated form.

    Args:
        raw_mac_address: A MAC address in any colon/hyphen/dot/no-separator
            style, in either case.

    Returns:
        The address as 6 upper-case hex octets joined by colons, e.g.
        ``"AA:BB:CC:DD:EE:FF"``.

    Raises:
        InvalidMacAddressError: *raw_mac_address* does not parse as a 6-octet
            MAC address.
    """
    candidate = (raw_mac_address or "").strip()
    if not _MAC_RE.match(candidate):
        raise InvalidMacAddressError(f"{raw_mac_address!r} is not a valid MAC address.")
    hex_only = re.sub(r"[:\-.]", "", candidate).upper()
    return ":".join(hex_only[i : i + 2] for i in range(0, 12, 2))
