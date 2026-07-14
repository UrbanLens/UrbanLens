"""ProfileLabelAssignment queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class ProfileLabelAssignmentQuerySet(abstract.DashboardQuerySet):
    """QuerySet for private user-label assignments on profiles."""


class ProfileLabelAssignmentManager(abstract.DashboardManager.from_queryset(ProfileLabelAssignmentQuerySet)):
    """Manager for ProfileLabelAssignment records."""
