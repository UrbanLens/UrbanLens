"""EmailVerification queryset and manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from urbanlens.dashboard.models.account.model import EmailVerification


class EmailVerificationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for email verification tokens."""


class EmailVerificationManager(abstract.DashboardManager.from_queryset(EmailVerificationQuerySet)):
    """Manager for EmailVerification records."""
