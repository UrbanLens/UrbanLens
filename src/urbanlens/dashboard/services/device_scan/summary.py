"""What the site shows for a device, summarised from every scan of it rather than taken from the latest one.

Each account counts once, by its latest report, so one uploader repeating itself cannot outvote others. An
unattributed upload has no account to fold it into, so each counts as its own reporter.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.device_scan.model import DeviceScanEntry, DeviceType, DeviceTypeSource
from urbanlens.dashboard.services.device_scan.type_guessing import guess_device_type

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.device_scan.model import ScannedDevice


def reporter(profile_id: int | None, upload_id: int) -> str:
    """The key one reporter's scans share: their account, or the upload itself when it is unattributed.

    Args:
        profile_id: The uploading profile, or None.
        upload_id: The upload.

    Returns:
        A key that is equal for every scan by the same reporter.
    """
    return f"profile:{profile_id}" if profile_id is not None else f"upload:{upload_id}"


def _plurality(latest: dict[str, str]) -> str | None:
    """The value most reporters gave, ties going to the one reported most recently."""
    if not latest:
        return None
    counts = Counter(latest.values())
    recency = {value: position for position, value in enumerate(latest.values())}
    return max(counts, key=lambda value: (counts[value], recency[value]))


def summarize_devices(devices: Iterable[ScannedDevice]) -> None:
    """Recompute each device's type and name from all of its scans, saving any that changed.

    Args:
        devices: The devices to summarise.
    """
    by_pk = {device.pk: device for device in devices}
    guesses: dict[int, dict[str, str]] = {pk: {} for pk in by_pk}
    names: dict[int, dict[str, str]] = {pk: {} for pk in by_pk}
    rows = DeviceScanEntry.objects.filter(device_id__in=by_pk).order_by("created", "pk").values_list("device_id", "upload__profile_id", "upload_id", "device_type_guess", "device_name")
    for device_id, profile_id, upload_id, guess, name in rows:
        key = reporter(profile_id, upload_id)
        if guess:
            guesses[device_id].pop(key, None)
            guesses[device_id][key] = guess
        if name:
            names[device_id].pop(key, None)
            names[device_id][key] = name

    for pk, device in by_pk.items():
        display_name = _plurality(names[pk]) or device.display_name
        votes = {key: guess for key, guess in guesses[pk].items() if guess != DeviceType.UNKNOWN}
        device_type = _plurality(votes)
        source = DeviceTypeSource.CLIENT
        if device_type is None:
            device_type, confidence = guess_device_type(mac_address=device.mac_address, display_name=display_name)
            source = DeviceTypeSource.HEURISTIC if confidence > 0 else DeviceTypeSource.UNSET
        if (device.display_name, device.device_type, device.device_type_source) != (display_name, device_type, source):
            device.display_name = display_name
            device.device_type = device_type
            device.device_type_source = source
            device.save(update_fields=["display_name", "device_type", "device_type_source", "updated"])
