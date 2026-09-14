from __future__ import annotations

from urbanlens.dashboard.models import abstract


class LinkQuerySet(abstract.DashboardQuerySet):
    """QuerySet shared by PinLink and WikiLink."""

    def needs_archiving(self):
        """Links not yet sent to the Wayback Machine."""
        return self.filter(wayback_url="")


class LinkManager(abstract.DashboardManager.from_queryset(LinkQuerySet)):
    pass
