"""QuerySet and Manager for ProxiedMediaRender."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.proxied_media_render.model import ProxiedMediaRender  # noqa: F401 - mypy needs it; ruff does not


class ProxiedMediaRenderQuerySet(abstract.DashboardQuerySet["ProxiedMediaRender"]):
    """QuerySet for ProxiedMediaRender."""


_ProxiedMediaRenderManagerBase = abstract.DashboardManager.from_queryset(ProxiedMediaRenderQuerySet)


class ProxiedMediaRenderManager(_ProxiedMediaRenderManagerBase):
    """Manager for ProxiedMediaRender."""
