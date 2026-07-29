"""Per-upload processing: classify devices, match wikis, update markers.

Called from ``dashboard.tasks.process_device_scan_upload`` - kept separate
from the task itself so the actual logic is independently testable without
Celery's task machinery, matching how every other background job in this app
splits a thin ``tasks.py`` wrapper from its real work in ``services/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload


def process_scan_upload(upload: DeviceScanUpload) -> None:
    """Classify every device in *upload* and update its wiki marker(s).

    For each entry: resolve the device's type (trusting a client guess,
    otherwise a heuristic - see ``type_guessing.resolve_device_type``); a
    positive (``detected=True``) reading of a security-relevant type
    (camera/sensor/tracker) triggers a marker recompute on every wiki whose
    boundary contains it, while a negative (``detected=False``) reading
    feeds the matched marker's absence streak.

    Args:
        upload: The upload to process. Callers should have its ``entries``
            (and each entry's ``device``/``expected_marker``) prefetched to
            avoid an N+1 query pattern.
    """
    from urbanlens.dashboard.models.device_scan.model import SECURITY_RELEVANT_TYPES, WikiDeviceMarker
    from urbanlens.dashboard.services.device_scan.clustering import MERGE_DISTANCE_METERS, recompute_wiki_device_markers, record_absence_report
    from urbanlens.dashboard.services.device_scan.type_guessing import resolve_device_type
    from urbanlens.dashboard.services.device_scan.wiki_lookup import wikis_containing_point

    for entry in upload.entries.all():
        device = entry.device
        device_type, device_type_source = resolve_device_type(
            current_type=device.device_type,
            current_source=device.device_type_source,
            client_guess=entry.device_type_guess or None,
            mac_address=device.mac_address,
            display_name=device.display_name,
        )
        if (device_type, device_type_source) != (device.device_type, device.device_type_source):
            device.device_type = device_type
            device.device_type_source = device_type_source
            device.save(update_fields=["device_type", "device_type_source", "updated"])

        if not entry.detected:
            marker = entry.expected_marker
            if marker is None:
                marker = WikiDeviceMarker.objects.near(entry.location, MERGE_DISTANCE_METERS).filter(device=device).first()
            if marker is not None:
                record_absence_report(marker)
            continue

        if device_type not in SECURITY_RELEVANT_TYPES:
            continue

        for wiki in wikis_containing_point(entry.location):
            recompute_wiki_device_markers(device, wiki)
