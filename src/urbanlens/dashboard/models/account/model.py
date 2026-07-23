"""Account-level auth models: email verification tokens and client-side KDF enrollment."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from urbanlens.dashboard.models.abstract import DashboardModel
from urbanlens.dashboard.models.abstract.choices import TextChoices
from urbanlens.dashboard.models.account.queryset import (
    AccountKdfManager,
    ApiKeyManager,
    ApiKeyUsageLogManager,
    BackupCodeManager,
    EmailVerificationManager,
    TOTPDeviceManager,
    WebAuthnCredentialManager,
)
from urbanlens.dashboard.models.fields import EncryptedTextField


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

    objects = AccountKdfManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_account_kdf"

    def __str__(self) -> str:
        return f"AccountKdf(user={self.user_id})"


class WebAuthnCredential(DashboardModel):
    """A registered passkey used as an optional second factor at login.

    An account with zero rows here logs in with a password alone. The moment
    it has one or more, ``CustomLoginView`` routes password logins through
    ``LoginTwoFactorView`` for a passkey assertion before establishing the
    session - 2FA is opt-in per user, never enforced site-wide. Users are
    expected to register more than one credential (e.g. a laptop's platform
    authenticator plus a password-manager-synced passkey like Bitwarden) so
    losing one device doesn't lock them out.

    ``credential_id``/``public_key`` are the raw bytes handed back by
    ``webauthn.verify_registration_response()`` - never decoded or displayed,
    only round-tripped through authentication ceremonies. ``sign_count`` lets
    ``verify_authentication_response()`` detect cloned authenticators; synced
    passkeys (Bitwarden, iCloud Keychain) typically report 0 forever, which
    the library treats as "not supported" rather than a replay.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webauthn_credentials")
    name = models.CharField(max_length=100, blank=True)
    credential_id = models.BinaryField(unique=True, editable=False)
    public_key = models.BinaryField(editable=False)
    sign_count = models.PositiveBigIntegerField(default=0)
    aaguid = models.CharField(max_length=64, blank=True, editable=False)
    device_type = models.CharField(max_length=16, blank=True, editable=False)
    backup_eligible = models.BooleanField(default=False, editable=False)
    # Authenticator transports reported at registration (e.g. "internal", "hybrid"),
    # used to populate allowCredentials hints on later authentication ceremonies.
    transports = models.JSONField(default=list, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        id: int
        user_id: int

    objects = WebAuthnCredentialManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_webauthn_credential"
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"WebAuthnCredential({self.user_id}, {self.name or self.pk})"


class TOTPDevice(DashboardModel):
    """An authenticator-app (RFC 6238 TOTP) second factor, alternative to a passkey.

    One per account, created only once the user has confirmed a code from
    their app - an unconfirmed setup lives only in the session (see
    ``TOTPSetupStartView``/``TOTPSetupConfirmView``) and never reaches the
    database. ``secret`` is encrypted at rest via ``EncryptedTextField``, the
    same mechanism already used for OAuth tokens (Flickr/Immich/Google
    Photos) elsewhere in this app.

    ``last_used_step`` blocks replay of an intercepted code: a verified
    login records the 30-second time-step it matched, and any later
    verification attempt for that same step (or an earlier one) is rejected
    even if the code is still numerically valid within the tolerance window.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="totp_device")
    secret = EncryptedTextField()
    last_used_step = models.BigIntegerField(null=True, blank=True)

    if TYPE_CHECKING:
        user_id: int

    objects = TOTPDeviceManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_totp_device"

    def __str__(self) -> str:
        return f"TOTPDevice(user={self.user_id})"


class BackupCode(DashboardModel):
    """A single-use recovery code for accounts with a passkey and/or TOTP device.

    Generated ten at a time (``services.two_factor.generate_backup_codes``),
    shown to the user exactly once in plaintext, and stored here only as a
    salted hash (``django.contrib.auth.hashers``) - like a password, the
    plaintext can never be recovered from the database. Codes are scoped to
    the account as a whole rather than to a specific factor, since they
    exist purely to unblock a login when every other factor is unavailable.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="backup_codes")
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        id: int
        user_id: int

    objects = BackupCodeManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_backup_code"

    def __str__(self) -> str:
        status = "used" if self.used_at else "unused"
        return f"BackupCode(user={self.user_id}, {status})"


class ApiKeyScope(TextChoices):
    """Capabilities an ``ApiKey`` can grant to the external application holding it."""

    PROFILE_READ = "profile:read", "Read your profile UUID"
    PINS_WRITE = "pins:write", "Create pins on your behalf"


def _default_api_key_scopes() -> list[str]:
    """The fixed grant every new key gets - there is no scope picker yet.

    Kept as a real per-row field (rather than an implicit "all keys can do
    everything" assumption) so a future scope-picker UI only has to change
    what gets written here; ``external_api.permissions`` already checks
    per-key scopes rather than trusting the mere existence of a valid key.
    """
    return [ApiKeyScope.PROFILE_READ.value, ApiKeyScope.PINS_WRITE.value]


class ApiKey(DashboardModel):
    """A user-issued credential letting an external application act on their behalf.

    Grants only what's listed in ``scopes`` - currently always both members of
    :class:`ApiKeyScope`, since there's no scope picker yet (every key can
    read the owner's uuid and create pins as them, and nothing else). Verified
    by ``services.api_keys.authenticate_api_key`` via
    ``external_api.authentication.ApiKeyAuthentication``, which is wired into
    the external API's viewsets only - it is never added to
    ``DEFAULT_AUTHENTICATION_CLASSES``, so it has no bearing on the internal,
    session-authenticated REST surface.

    Only a salted hash of the key's secret half is stored (``key_hash``, via
    ``django.contrib.auth.hashers`` - the same hash-never-store-plaintext
    pattern as :class:`BackupCode`). ``prefix`` is the *public* half, stored
    in plaintext so ``authenticate_api_key`` can look up the owning row in
    O(1) before hashing, rather than iterating every active key on every
    request - unlike backup codes (bounded at ~10/user), a user may
    accumulate many keys over time, and Django's password hasher is
    deliberately slow.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100, help_text='User-facing label, e.g. "Zapier".')
    prefix = models.CharField(max_length=12, unique=True, editable=False)
    key_hash = models.CharField(max_length=128, editable=False)
    scopes = models.JSONField(default=_default_api_key_scopes, editable=False)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    if TYPE_CHECKING:
        id: int
        user_id: int

    objects = ApiKeyManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_api_key"
        ordering = ["-created"]

    def __str__(self) -> str:
        status = "revoked" if self.revoked_at else "active"
        return f"ApiKey(user={self.user_id}, prefix={self.prefix}, {status})"

    @property
    def is_revoked(self) -> bool:
        """True once this key has been revoked and can no longer authenticate."""
        return self.revoked_at is not None


class ApiKeyUsageLog(DashboardModel):
    """A recent-activity trail for one ``ApiKey`` - what it's actually been used for.

    Written once per successfully authenticated external-API request (see
    ``services.api_keys.record_api_key_usage``, called from
    ``external_api.authentication.ApiKeyAuthentication``) - never for a
    failed/unauthenticated attempt, so this can't be used to fingerprint
    guessing attacks. Deliberately bounded rather than an unbounded audit
    log: each write trims the same key's rows back down to
    ``services.api_keys.USAGE_LOG_LIMIT``, since this exists for a user to
    sanity-check "what has this app been doing" in the settings UI, not as a
    compliance-grade record.
    """

    api_key = models.ForeignKey(ApiKey, on_delete=models.CASCADE, related_name="usage_log")
    endpoint = models.CharField(max_length=255)

    if TYPE_CHECKING:
        id: int
        api_key_id: int

    objects = ApiKeyUsageLogManager()

    class Meta(DashboardModel.Meta):
        db_table = "dashboard_api_key_usage_log"
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"ApiKeyUsageLog(api_key={self.api_key_id}, endpoint={self.endpoint})"


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
