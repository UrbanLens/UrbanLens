"""QuerySet and manager for GooglePlace."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.google_place.model import GooglePlace  # noqa: F401 - mypy needs these; ruff does not


class GooglePlaceQuerySet(abstract.DashboardQuerySet["GooglePlace"]):
    """Query helpers for Google Place cache rows."""


_GooglePlaceManagerBase = abstract.DashboardManager.from_queryset(GooglePlaceQuerySet)


class GooglePlaceManager(_GooglePlaceManagerBase):
    """Manager for GooglePlace."""
