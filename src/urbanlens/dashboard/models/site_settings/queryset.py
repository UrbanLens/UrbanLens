"""SiteSettings queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.site_settings.model import SiteSettings


class SiteSettingsQuerySet(abstract.QuerySet):
    """QuerySet for the site settings singleton."""


class SiteSettingsManager(abstract.Manager.from_queryset(SiteSettingsQuerySet)):
    """Manager for SiteSettings. Use get_current() for the singleton record."""

    def get_current(self) -> SiteSettings:
        """Return (and create if missing) the singleton settings record.

        Returns:
            The single SiteSettings row (pk=1).
        """
        obj, _ = self.get_or_create(pk=1)
        return obj
