"""ProfileLabelAssignment queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.profile_assignment.model import ProfileLabelAssignment  # noqa: F401 - mypy needs these; ruff does not


class ProfileLabelAssignmentQuerySet(abstract.DashboardQuerySet["ProfileLabelAssignment"]):
    """QuerySet for private user-label assignments."""


_ProfileLabelAssignmentManagerBase = abstract.DashboardManager.from_queryset(ProfileLabelAssignmentQuerySet)


class ProfileLabelAssignmentManager(_ProfileLabelAssignmentManagerBase):
    """Manager for ProfileLabelAssignment."""
