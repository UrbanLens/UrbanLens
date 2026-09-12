"""Account-model querysets and managers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models import abstract

if TYPE_CHECKING:
    from django.contrib.auth.models import User

    from urbanlens.dashboard.models.account.model import AccountKdf, ApiKey


class EmailVerificationQuerySet(abstract.DashboardQuerySet):
    """QuerySet for email verification tokens."""


class EmailVerificationManager(abstract.DashboardManager.from_queryset(EmailVerificationQuerySet)):
    """Manager for EmailVerification records."""


class AccountKdfQuerySet(abstract.DashboardQuerySet):
    """QuerySet for AccountKdf rows."""

    def for_user(self, user: User) -> AccountKdfQuerySet:
        """This user's KDF enrollment row, if any."""
        return self.filter(user=user)


class AccountKdfManager(abstract.DashboardManager.from_queryset(AccountKdfQuerySet)):
    """Manager for AccountKdf records."""

    def set_auth_salt(self, user: User, auth_salt: str) -> tuple[AccountKdf, bool]:
        """Enroll (or update) a user's client-side KDF auth salt."""
        return self.update_or_create(user=user, defaults={"auth_salt": auth_salt})


class WebAuthnCredentialQuerySet(abstract.DashboardQuerySet):
    """QuerySet for WebAuthnCredential rows."""

    def for_user(self, user: User) -> WebAuthnCredentialQuerySet:
        """This user's registered passkeys."""
        return self.filter(user=user)


class WebAuthnCredentialManager(abstract.DashboardManager.from_queryset(WebAuthnCredentialQuerySet)):
    """Manager for WebAuthnCredential records."""


class TOTPDeviceQuerySet(abstract.DashboardQuerySet):
    """QuerySet for TOTPDevice rows."""

    def for_user(self, user: User) -> TOTPDeviceQuerySet:
        """This user's TOTP device row, if any."""
        return self.filter(user=user)


class TOTPDeviceManager(abstract.DashboardManager.from_queryset(TOTPDeviceQuerySet)):
    """Manager for TOTPDevice records."""


class BackupCodeQuerySet(abstract.DashboardQuerySet):
    """QuerySet for BackupCode rows."""

    def for_user(self, user: User) -> BackupCodeQuerySet:
        """All of this user's backup codes."""
        return self.filter(user=user)

    def unused_for(self, user: User) -> BackupCodeQuerySet:
        """This user's not-yet-used backup codes."""
        return self.for_user(user).filter(used_at__isnull=True)


class BackupCodeManager(abstract.DashboardManager.from_queryset(BackupCodeQuerySet)):
    """Manager for BackupCode records."""


class ApiKeyQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ApiKey rows."""

    def for_user(self, user: User) -> ApiKeyQuerySet:
        """All of this user's API keys."""
        return self.filter(user=user)

    def active(self) -> ApiKeyQuerySet:
        """Keys that have not been revoked."""
        return self.filter(revoked_at__isnull=True)


class ApiKeyManager(abstract.DashboardManager.from_queryset(ApiKeyQuerySet)):
    """Manager for ApiKey records."""


class ApiKeyUsageLogQuerySet(abstract.DashboardQuerySet):
    """QuerySet for ApiKeyUsageLog rows."""

    def for_api_key(self, api_key: ApiKey) -> ApiKeyUsageLogQuerySet:
        """This key's logged activity, newest first."""
        return self.filter(api_key=api_key)


class ApiKeyUsageLogManager(abstract.DashboardManager.from_queryset(ApiKeyUsageLogQuerySet)):
    """Manager for ApiKeyUsageLog records."""
