from __future__ import annotations

from urbanlens.dashboard.models import abstract


class LinkQuerySet(abstract.DashboardQuerySet):
    """QuerySet shared by PinLink and WikiLink."""


class LinkManager(abstract.DashboardManager.from_queryset(LinkQuerySet)):
    pass
