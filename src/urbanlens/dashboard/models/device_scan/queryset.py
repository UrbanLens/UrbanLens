"""QuerySets and Managers for the device-scanning models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from django.contrib.gis.measure import D

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.contrib.gis.geos import Point

    from urbanlens.dashboard.models.device_scan.model import ScannedDevice
    from urbanlens.dashboard.models.wiki.model import Wiki


class ScannedDeviceQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for ScannedDevice."""


class ScannedDeviceManager(abstract.FrontendDashboardManager.from_queryset(ScannedDeviceQuerySet)):
    """Manager for ScannedDevice."""

    def get_or_create_for_mac(self, raw_mac_address: str) -> tuple[ScannedDevice, bool]:
        """Get or create the device identified by *raw_mac_address*, normalizing first.

        The single entry point for resolving a device by MAC - callers never
        normalize (or skip normalizing) on their own, which would otherwise
        risk two rows for the same physical device over a casing/separator
        difference between uploads.

        Args:
            raw_mac_address: MAC address as submitted by the client, in any
                case/separator style ``normalize_mac_address`` accepts.

        Returns:
            ``(device, created)``, exactly like ``get_or_create``.
        """
        from urbanlens.dashboard.services.device_scan.mac_address import normalize_mac_address

        return self.get_or_create(mac_address=normalize_mac_address(raw_mac_address))


class DeviceScanUploadQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for DeviceScanUpload."""

    def pending(self) -> Self:
        """Uploads not yet processed - a sweep target if the enqueue itself was ever lost."""
        from urbanlens.dashboard.models.device_scan.model import ScanUploadStatus

        return self.filter(status=ScanUploadStatus.PENDING)


class DeviceScanUploadManager(abstract.FrontendDashboardManager.from_queryset(DeviceScanUploadQuerySet)):
    """Manager for DeviceScanUpload."""


class DeviceScanEntryQuerySet(abstract.DashboardQuerySet):
    """QuerySet for DeviceScanEntry."""

    def for_device(self, device: ScannedDevice) -> Self:
        """Every entry ever submitted for *device*, across all uploads."""
        return self.filter(device=device)


class DeviceScanEntryManager(abstract.DashboardManager.from_queryset(DeviceScanEntryQuerySet)):
    """Manager for DeviceScanEntry."""


class DeviceSignalReadingQuerySet(abstract.DashboardQuerySet):
    """QuerySet for DeviceSignalReading."""


class DeviceSignalReadingManager(abstract.DashboardManager.from_queryset(DeviceSignalReadingQuerySet)):
    """Manager for DeviceSignalReading."""


class WikiDeviceMarkerQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for WikiDeviceMarker."""

    def visible(self) -> Self:
        """Markers worth telling the mobile app about (excludes dismissed/presumed-removed)."""
        from urbanlens.dashboard.models.device_scan.model import MarkerStatus

        return self.filter(status__in=(MarkerStatus.ACTIVE, MarkerStatus.STALE))

    def for_wiki_and_device(self, wiki: Wiki, device: ScannedDevice) -> Self:
        """Non-dismissed markers for one (wiki, device) pair, for recompute reconciliation."""
        from urbanlens.dashboard.models.device_scan.model import MarkerStatus

        return self.filter(wiki=wiki, device=device).exclude(status=MarkerStatus.DISMISSED)

    def near(self, point: Point, radius_meters: float) -> Self:
        """Markers whose centroid falls within *radius_meters* of *point*."""
        return self.filter(centroid__distance_lte=(point, D(m=radius_meters)))


class WikiDeviceMarkerManager(abstract.FrontendDashboardManager.from_queryset(WikiDeviceMarkerQuerySet)):
    """Manager for WikiDeviceMarker."""
