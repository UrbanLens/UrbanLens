"""SiteSettings queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.site_settings.model import SiteSettings


class SiteSettingsQuerySet(abstract.FrontendDashboardQuerySet):
    """QuerySet for the site settings singleton."""


class SiteSettingsManager(abstract.FrontendDashboardManager.from_queryset(SiteSettingsQuerySet)):
    """Manager for SiteSettings. Use get_current() for the singleton record."""

    def get_current(self) -> SiteSettings:
        """Return (and create if missing) the singleton settings record.

        Memoised for the duration of a request (see
        :mod:`urbanlens.dashboard.models.site_settings.request_cache`) - the row cannot
        change mid-request, and this is called several times over on every page. Outside
        a request the memo is inert and every call reads through to the database.

        Returns:
            The single SiteSettings row (pk=1).
        """
        from urbanlens.dashboard.models.site_settings import request_cache

        cached = request_cache.get_cached()
        if cached is not None:
            return cached

        obj, _ = self.get_or_create(pk=1)
        request_cache.set_cached(obj)
        return obj
