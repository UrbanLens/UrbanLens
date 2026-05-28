"""Email verification token model for new user registrations."""
from __future__ import annotations

import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class EmailVerification(models.Model):
    """One-time token used to verify a new user's email address.

    Created when a user registers via email/password.  SSO users skip this
    entirely since their email is implicitly verified by the OAuth provider.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="email_verification")
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "dashboard_email_verification"

    def __str__(self) -> str:
        return f"EmailVerification({self.user.username})"

    def is_valid(self) -> bool:
        """True if not yet verified and within the 48-hour window."""
        if self.verified_at:
            return False
        return timezone.now() < self.created + timezone.timedelta(hours=48)

    def mark_verified(self) -> None:
        """Record the verification timestamp."""
        self.verified_at = timezone.now()
        self.save(update_fields=["verified_at"])
