from __future__ import annotations

from urbanlens.dashboard.models import abstract


class LinkQuerySet(abstract.DashboardQuerySet):
    """QuerySet shared by PinLink and WikiLink."""


_LinkManagerBase = abstract.DashboardManager.from_queryset(LinkQuerySet)


class LinkManager(_LinkManagerBase):
    pass
