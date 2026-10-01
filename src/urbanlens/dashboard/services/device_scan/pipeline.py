"""Per-upload processing: re-summarise each device, then update the markers of wikis the upload may reach."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.device_scan.model import DeviceScanUpload


def process_scan_upload(upload: DeviceScanUpload) -> None:
    """Summarise every device in *upload* and update its wiki marker(s).

    Args:
        upload: The upload to process."""
    from urbanlens.dashboard.models.device_scan.model import SECURITY_RELEVANT_TYPES, WikiDeviceMarker
    from urbanlens.dashboard.services.device_scan.clustering import MERGE_DISTANCE_METERS, recompute_wiki_device_markers, recount_absence_reports
    from urbanlens.dashboard.services.device_scan.summary import summarize_devices
    from urbanlens.dashboard.services.device_scan.wiki_lookup import wikis_containing_point

    entries = list(upload.entries.select_related("device", "expected_marker"))
    devices = {entry.device_id: entry.device for entry in entries}
    summarize_devices(devices.values())
    routable = set(upload.routable_wikis.values_list("pk", flat=True)) if upload.routing_recorded else None

    for entry in entries:
        device = devices[entry.device_id]
        if not entry.detected:
            marker = entry.expected_marker
            if marker is None:
                nearby = WikiDeviceMarker.objects.near(entry.location, MERGE_DISTANCE_METERS).filter(device=device)
                marker = (nearby if routable is None else nearby.filter(wiki_id__in=routable)).first()
            if marker is not None and (routable is None or marker.wiki_id in routable):
                recount_absence_reports(marker)
            continue

        if device.device_type not in SECURITY_RELEVANT_TYPES:
            continue

        for wiki in wikis_containing_point(entry.location):
            if routable is None or wiki.pk in routable:
                recompute_wiki_device_markers(device, wiki)
