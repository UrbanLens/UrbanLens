"""QuerySet/Manager for registered native push devices."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.push_device.model import PushDevice  # noqa: F401 - mypy needs these; ruff does not


class PushDeviceQuerySet(abstract.FrontendDashboardQuerySet["PushDevice"]):
    """QuerySet for :class:`~urbanlens.dashboard.models.push_device.model.PushDevice`."""

    def active(self) -> PushDeviceQuerySet:
        """Restrict to devices that can still receive pushes."""
        return self.filter(revoked_at__isnull=True)

    def for_profile(self, profile: Profile) -> PushDeviceQuerySet:
        """Restrict to one profile's registered devices.

        Args:
            profile: The owning profile.

        Returns:
            This queryset filtered to the profile's devices.
        """
        return self.filter(profile=profile)


_PushDeviceManagerBase = abstract.FrontendDashboardManager.from_queryset(PushDeviceQuerySet)


class PushDeviceManager(_PushDeviceManagerBase):
    """Manager for :class:`~urbanlens.dashboard.models.push_device.model.PushDevice`."""
