"""Per-upload processing: classify devices, match wikis, update markers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload


def process_scan_upload(upload: DeviceScanUpload) -> None:
    """Classify every device in *upload* and update its wiki marker(s).

    Args:
        upload: The upload to process."""
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
