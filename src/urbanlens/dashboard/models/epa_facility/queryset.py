"""QuerySet and manager for EpaFacility."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.epa_facility.model import EpaFacility  # noqa: F401 - mypy needs these; ruff does not


class EpaFacilityQuerySet(abstract.DashboardQuerySet["EpaFacility"]):
    """Query helpers for EPA ECHO facility records."""


_EpaFacilityManagerBase = abstract.DashboardManager.from_queryset(EpaFacilityQuerySet)


class EpaFacilityManager(_EpaFacilityManagerBase):
    """Manager for EpaFacility."""
