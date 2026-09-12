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
    When this row exists, the browser derives the credential sent at login (``authKey``) from the raw password via Argon2id + ``auth_salt``, and the server's stored password hash is a hash of that derived key - the raw password never reaches the server.
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
    An account with zero rows here logs in with a password alone.
    The moment it has one or more, ``CustomLoginView`` routes password logins through ``LoginTwoFactorView`` for a passkey assertion before establishing the session - 2FA is opt-in per user, never enforced site-wide.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webauthn_credentials")
    name = models.CharField(max_length=100, blank=True)
    credential_id = models.BinaryField(unique=True, editable=False)
    public_key = models.BinaryField(editable=False)
    # False for passkeys enrolled only to unlock E2EE data (see E2EEPasskeyWrap): those must never
    # flip the account into mandatory 2FA, or "add a passkey so your messages unlock anywhere" would
    # conscript the user into a login prompt on every signin.
    # Every 2FA gate filters on this - the single source of truth is
    is_login_factor = models.BooleanField(default=True)
    sign_count = models.PositiveBigIntegerField(default=0)
    aaguid = models.CharField(max_length=64, blank=True, editable=False)
    device_type = models.CharField(max_length=16, blank=True, editable=False)
    backup_eligible = models.BooleanField(default=False, editable=False)
    # Authenticator transports reported at registration (e.g. "internal", "hybrid"),
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
    One per account, created only once the user has confirmed a code from their app - an unconfirmed setup lives only in the session (see ``TOTPSetupStartView``/``TOTPSetupConfirmView``) rather than this table.
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
    Generated ten at a time (``services.auth.two_factor.generate_backup_codes``), shown to the user exactly once in plaintext, and stored here only as a salted hash (``django.contrib.auth.hashers``) - like a password, the plaintext can never be recovered from the database.
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
    """Capabilities a credential can grant to the external application holding it.
    The single source of truth for the external API's scope vocabulary, shared by both credential kinds (PAT-style :class:`ApiKey` rows and django-oauth-toolkit access tokens).
    Names are *domain*-shaped (``<domain>:<action>``) rather than endpoint-shaped so a domain can grow new endpoints without minting new scopes - a user who consented to "read your trips" does not need to re-consent when a trip-stops endpoint ships.
    """

    PROFILE_READ = "profile:read", "Read your profile UUID"
    SETTINGS_READ = "settings:read", "Read your account preferences"
    SETTINGS_WRITE = "settings:write", "Change your account preferences"
    PINS_READ = "pins:read", "Read your pins (including deletions, for sync)"
    PINS_WRITE = "pins:write", "Create, edit, and delete your pins"
    LISTS_READ = "lists:read", "Read your pin lists and saved filters"
    LISTS_WRITE = "lists:write", "Create and modify your pin lists and saved filters"
    LABELS_READ = "labels:read", "Read your labels"
    LABELS_WRITE = "labels:write", "Create, modify, and merge your labels"
    VISITS_READ = "visits:read", "Read your visit history"
    VISITS_WRITE = "visits:write", "Log visits on your behalf"
    PHOTOS_READ = "photos:read", "Read your photos, memories journal, and photo suggestions"
    PHOTOS_WRITE = "photos:write", "Upload, label, file, vote on, and delete your photos, and act on photo suggestions"
    MEDIA_READ = "media:read", "Fetch the actual image/video/document files you may see"
    WIKI_READ = "wiki:read", "Read community wikis you can see"
    WIKI_WRITE = "wiki:write", "Edit community wikis on your behalf"
    TRIPS_READ = "trips:read", "Read your trips"
    TRIPS_WRITE = "trips:write", "Create and edit your trips"
    SOCIAL_READ = "social:read", "Read your friends list and friend requests"
    SOCIAL_WRITE = "social:write", "Send, accept, and manage friend relationships on your behalf"
    SAFETY_READ = "safety:read", "Read your safety check-ins and contacts"
    SAFETY_WRITE = "safety:write", "Start, update, and clear safety check-ins"
    MESSAGES_READ = "messages:read", "Read your encrypted messages and conversation list"
    MESSAGES_WRITE = "messages:write", "Send messages and manage your encryption keys"
    NOTIFICATIONS_READ = "notifications:read", "Read your notifications and delivery preferences"
    NOTIFICATIONS_WRITE = "notifications:write", "Mark notifications read and change delivery preferences"
    SEARCH_READ = "search:read", "Search your pins, wikis, and photos"
    # Covers every built-in game (SpotGuessr today, Trivia and Consensus next): one domain, because
    # a player who consented to "play games as you" should not have to re-consent when a second game
    # ships.
    GAMES_READ = "games:read", "Read your game history, scores, and leaderboard standing"
    GAMES_WRITE = "games:write", "Start games and submit guesses and answers on your behalf"
    PUSH_MANAGE = "push:manage", "Register and remove this device's push notifications"
    CUSTOM_FIELDS_READ = "custom_fields:read", "Read your custom field definitions and their values"
    CUSTOM_FIELDS_WRITE = "custom_fields:write", "Create, edit, and delete your custom fields and their values"
    UNDO_READ = "undo:read", "Read your recent delete history available to undo"
    UNDO_WRITE = "undo:write", "Restore a previously deleted item"
    PANELS_READ = "panels:read", "Read pin-detail enrichment panels (boundaries and other plugin-contributed data)"
    ASSISTANT_WRITE = "assistant:write", "Chat with your AI assistant, including creating trips and trip activities it suggests"
    DEVICE_SCANS_READ = "device_scans:read", "Read nearby expected devices and their signal info"
    DEVICE_SCANS_WRITE = "device_scans:write", "Upload wireless device scan data"


def _default_api_key_scopes() -> list[str]:
    """The fixed grant every new PAT-style key gets - there is no scope picker yet.
    Silently expanding every already-issued key's grant to cover messages, safety check-ins, photos and the rest would hand integrations a reach their owner never consented to - a privilege escalation, not a convenience.

    Returns:
        The four scope values every newly issued API key is granted.
    """
    return [ApiKeyScope.PROFILE_READ.value, ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value, ApiKeyScope.PUSH_MANAGE.value]


class ApiKey(DashboardModel):
    """A user-issued credential letting an external application act on their behalf.
    Grants only what's listed in ``scopes`` - currently always both members of :class:`ApiKeyScope`, since there's no scope picker yet (every key can read the owner's uuid and create pins as them, and nothing else).
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
    """A recent-activity trail for one ``ApiKey`` - what it's actually been used for."""

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
    """One-time token used to verify a new user's email address. Created when a user registers via email/password."""

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
