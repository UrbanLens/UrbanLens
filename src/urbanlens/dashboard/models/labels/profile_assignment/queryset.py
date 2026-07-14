"""ProfileBadgeAssignment queryset and manager."""

from __future__ import annotations

from urbanlens.dashboard.models import abstract


class ProfileBadgeAssignmentQuerySet(abstract.DashboardQuerySet):
    """QuerySet for private user-badge assignments on profiles."""


class ProfileBadgeAssignmentManager(abstract.DashboardManager.from_queryset(ProfileBadgeAssignmentQuerySet)):
    """Manager for ProfileBadgeAssignment records."""
