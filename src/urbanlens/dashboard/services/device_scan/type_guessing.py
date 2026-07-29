"""Best-effort wireless-device type classification.

A client-supplied ``device_type_guess`` is always trusted over anything this
module computes (see :func:`resolve_device_type`) - "generally speaking,
trust the guessed device type they provide" - this module only fills the gap
when a client sends no guess and the device has never been classified at
all. Two independent, deliberately modest-confidence signals are tried, name
first (a manufacturer's own product naming, e.g. "Wyze Cam", is usually more
specific than a MAC vendor prefix alone can tell you): a device-name
substring match, then a MAC-OUI vendor table. Neither is authoritative - a
miss just leaves the device UNKNOWN pending a client guess, which is always
the safe default; a wrong guess would not be.
"""

from __future__ import annotations

from urbanlens.dashboard.models.device_scan.model import DeviceType, DeviceTypeSource

#: Case-insensitive substrings checked against a device's advertised
#: name/SSID. The first match wins - kept short and specific rather than
#: exhaustive.
_NAME_SUBSTRINGS: tuple[tuple[str, DeviceType], ...] = (
    ("nest cam", DeviceType.CAMERA),
    ("nest doorbell", DeviceType.CAMERA),
    ("wyze cam", DeviceType.CAMERA),
    ("ring doorbell", DeviceType.CAMERA),
    ("ring cam", DeviceType.CAMERA),
    ("arlo", DeviceType.CAMERA),
    ("blink", DeviceType.CAMERA),
    ("hikvision", DeviceType.CAMERA),
    ("dahua", DeviceType.CAMERA),
    ("reolink", DeviceType.CAMERA),
    ("foscam", DeviceType.CAMERA),
    ("eufycam", DeviceType.CAMERA),
    ("ipcam", DeviceType.CAMERA),
    ("nvr", DeviceType.CAMERA),
    ("airtag", DeviceType.TRACKER),
    ("tile", DeviceType.TRACKER),
    ("smarttag", DeviceType.TRACKER),
    ("chipolo", DeviceType.TRACKER),
    ("motion sensor", DeviceType.SENSOR),
    ("door sensor", DeviceType.SENSOR),
    ("smartthings", DeviceType.SENSOR),
)

#: MAC OUI prefixes (first 3 octets, colon-separated, upper-case) for
#: vendors whose product line is overwhelmingly one category. Deliberately a
#: small, curated table rather than a full IEEE OUI database.
_OUI_TYPES: dict[str, DeviceType] = {
    "8C:C8:F4": DeviceType.CAMERA,  # Hikvision
    "BC:AD:28": DeviceType.CAMERA,  # Hikvision
    "3C:EF:8C": DeviceType.CAMERA,  # Dahua
    "9C:8E:CD": DeviceType.CAMERA,  # Dahua
    "B0:C5:54": DeviceType.CAMERA,  # Reolink
    "2C:AA:8E": DeviceType.CAMERA,  # Wyze
    "74:B5:87": DeviceType.CAMERA,  # Amazon (Ring/Blink)
    "18:B4:30": DeviceType.SENSOR,  # Nest Labs
    "64:16:66": DeviceType.SENSOR,  # Nest Labs
    "F0:81:73": DeviceType.SENSOR,  # Espressif (common IoT sensor modules)
    "24:6F:28": DeviceType.SENSOR,  # Espressif
}

#: Fixed, deliberately modest confidence for any heuristic match - this is a
#: guess, never a certainty, and must never be mistaken for a CLIENT-sourced
#: classification.
_HEURISTIC_CONFIDENCE = 0.35


def guess_device_type(*, mac_address: str, display_name: str) -> tuple[str, float]:
    """Best-effort device-type guess from a name and MAC address.

    Args:
        mac_address: Normalized (colon-separated, upper-case) MAC address.
        display_name: Advertised device name/SSID, or "".

    Returns:
        ``(DeviceType.UNKNOWN, 0.0)`` when nothing matched, otherwise the
        matched type and :data:`_HEURISTIC_CONFIDENCE`.
    """
    lowered = (display_name or "").lower()
    for substring, device_type in _NAME_SUBSTRINGS:
        if substring in lowered:
            return device_type, _HEURISTIC_CONFIDENCE

    oui = mac_address[:8].upper()
    oui_device_type = _OUI_TYPES.get(oui)
    if oui_device_type is not None:
        return oui_device_type, _HEURISTIC_CONFIDENCE

    return DeviceType.UNKNOWN, 0.0


def resolve_device_type(
    *,
    current_type: str,
    current_source: str,
    client_guess: str | None,
    mac_address: str,
    display_name: str,
) -> tuple[str, str]:
    """Decide a ScannedDevice's device_type/device_type_source for this scan round.

    A client-supplied guess always wins, overwriting any prior heuristic
    classification. Absent a guess, the heuristic only runs while the device
    has never been classified at all (``current_source ==
    DeviceTypeSource.UNSET``); once heuristic- or client-classified, a later
    scan with no guess of its own leaves the existing classification alone
    rather than flip-flopping on every upload.

    Args:
        current_type: The device's current ``device_type`` value.
        current_source: The device's current ``device_type_source`` value.
        client_guess: This round's client-supplied guess, or None/empty.
        mac_address: Normalized MAC address, for the heuristic fallback.
        display_name: Advertised device name, for the heuristic fallback.

    Returns:
        ``(device_type, device_type_source)`` to persist.
    """
    if client_guess:
        return client_guess, DeviceTypeSource.CLIENT

    if current_source == DeviceTypeSource.UNSET:
        guessed_type, confidence = guess_device_type(mac_address=mac_address, display_name=display_name)
        if confidence > 0:
            return guessed_type, DeviceTypeSource.HEURISTIC

    return current_type, current_source
