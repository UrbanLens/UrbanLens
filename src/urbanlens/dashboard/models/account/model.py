"""Account-level auth models: email verification tokens and client-side KDF enrollment."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from urbanlens.dashboard.models.abstract import DashboardModel
from urbanlens.dashboard.models.account.queryset import EmailVerificationManager


class AccountKdf(DashboardModel):
    """Marks an account as using client-side derived authentication.

    When this row exists, the browser derives the credential sent at login
    (``authKey``) from the raw password via Argon2id + ``auth_salt``, and the
    server's stored password hash is a hash of that derived key - the raw
    password never reaches the server. Accounts without a row authenticate
    with the raw password as usual ("legacy" mode) and are upgraded
    transparently on their next successful login.

    ``auth_salt`` is deliberately independent of
    ``MessagingKeyBundle.password_wrap_salt`` so the authentication credential
    and the key-wrapping key are cryptographically separated - knowing one
    derivation reveals nothing about the other.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="account_kdf")
    # Argon2id salt (base64) for deriving the login credential client-side.
    auth_salt = models.CharField(max_length=64)

    if TYPE_CHECKING:
        user_id: int

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_account_kdf"

    def __str__(self) -> str:
        return f"AccountKdf(user={self.user_id})"


class EmailVerification(DashboardModel):
    """One-time token used to verify a new user's email address.

    Created when a user registers via email/password.  SSO users skip this
    entirely since their email is implicitly verified by the OAuth provider.
    """

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    pending_invite_token = models.UUIDField(null=True, blank=True)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="email_verification")

    if TYPE_CHECKING:
        user_id: int

    objects = EmailVerificationManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_email_verification"

    def __str__(self) -> str:
        return f"EmailVerification({self.user.username})"

    def is_valid(self) -> bool:
        """True if not yet verified and within the 48-hour window."""
        if self.verified_at:
            return False
        return timezone.now() < self.created + timedelta(hours=48)

    def mark_verified(self) -> None:
        """Record the verification timestamp."""
        self.verified_at = timezone.now()
        self.save(update_fields=["verified_at"])
