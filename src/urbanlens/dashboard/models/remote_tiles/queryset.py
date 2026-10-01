"""QuerySets and Managers for RemoteTileSource and RemoteTile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.remote_tiles.model import RemoteTile, RemoteTileSource  # noqa: F401 - mypy needs it; ruff does not


class RemoteTileSourceQuerySet(abstract.DashboardQuerySet["RemoteTileSource"]):
    """QuerySet for RemoteTileSource."""


_RemoteTileSourceManagerBase = abstract.DashboardManager.from_queryset(RemoteTileSourceQuerySet)


class RemoteTileSourceManager(_RemoteTileSourceManagerBase):
    """Manager for RemoteTileSource."""


class RemoteTileQuerySet(abstract.DashboardQuerySet["RemoteTile"]):
    """QuerySet for RemoteTile."""


_RemoteTileManagerBase = abstract.DashboardManager.from_queryset(RemoteTileQuerySet)


class RemoteTileManager(_RemoteTileManagerBase):
    """Manager for RemoteTile."""
