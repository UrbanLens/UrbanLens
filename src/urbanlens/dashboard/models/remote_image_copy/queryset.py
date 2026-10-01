"""QuerySet and Manager for RemoteImageCopy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy  # noqa: F401 - mypy needs it; ruff does not


class RemoteImageCopyQuerySet(abstract.DashboardQuerySet["RemoteImageCopy"]):
    """QuerySet for RemoteImageCopy."""


_RemoteImageCopyManagerBase = abstract.DashboardManager.from_queryset(RemoteImageCopyQuerySet)


class RemoteImageCopyManager(_RemoteImageCopyManagerBase):
    """Manager for RemoteImageCopy."""
