from __future__ import annotations

from urbanlens.dashboard.models import abstract


class LinkQuerySet(abstract.DashboardQuerySet):
    """QuerySet shared by PinLink and WikiLink."""

    def needs_archiving(self):
        """Links that haven't been sent to the Wayback Machine yet."""
        return self.filter(wayback_url="")


class LinkManager(abstract.DashboardManager.from_queryset(LinkQuerySet)):
    pass
