"""Per-profile encryption key bundle - the server-side anchor of DM end-to-end encryption.

Every blob stored here was encrypted (or generated) in the user's browser;
the server never sees the private key, the wrapping keys, or the passwords
they derive from. See ``docs/e2ee.md`` for the full format specification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, BooleanField, CharField, IntegerField, OneToOneField, PositiveIntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.e2ee.queryset import MessagingKeyBundleManager

#: Argon2id defaults matching libsodium's crypto_pwhash "interactive" limits.
#: Pinned per-bundle so parameter upgrades never orphan existing blobs.
DEFAULT_KDF_OPSLIMIT = 2
DEFAULT_KDF_MEMLIMIT = 67_108_864  # 64 MiB


class MessagingKeyBundle(abstract.DashboardModel):
    """One profile's public key plus wrapped (client-encrypted) copies of its private key.

    Unwrap paths, in the order clients try them:

    1. ``password_wrapped_secret`` - private key encrypted under an Argon2id
       key derived from the login password in the browser (never transmitted).
       Empty for OAuth-only accounts, which have no password.
    2. ``recovery_wrapped_secret`` - private key encrypted under a random
       32-byte recovery key shown to the user once at enrollment. This is the
       only path for OAuth users on a new device and for password users after
       an email-link password reset on a device with no cached key.

    ``password_wrap_stale`` is set when a password reset invalidates path 1;
    any client that still holds the decrypted private key clears it by
    re-wrapping under the new password (``POST e2ee/rewrap``).
    """

    profile = OneToOneField(
        "dashboard.Profile",
        on_delete=CASCADE,
        related_name="messaging_key_bundle",
    )

    # X25519 public key, base64. World-readable by design (gated to profiles
    # that could message this user, to avoid a public key-harvesting endpoint).
    public_key = CharField(max_length=128)

    # crypto_secretbox blob (base64: nonce || ciphertext) of the private key,
    # wrapped under the password-derived key. Empty for OAuth-only accounts.
    password_wrapped_secret = TextField(blank=True, default="")
    # Argon2id salt (base64) used to derive the wrapping key from the password.
    # Independent from AccountKdf.auth_salt - domain separation between the
    # authentication credential and the wrapping key.
    password_wrap_salt = CharField(max_length=64, blank=True, default="")
    # True when a password reset made password_wrapped_secret undecryptable
    # (the old password is gone). Cleared by the next successful rewrap.
    password_wrap_stale = BooleanField(default=False)

    # crypto_secretbox blob (base64) of the private key, wrapped directly
    # under the full-entropy recovery key (no KDF needed).
    recovery_wrapped_secret = TextField()

    # Argon2id parameters the wrapping keys were derived with.
    kdf_opslimit = IntegerField(default=DEFAULT_KDF_OPSLIMIT)
    kdf_memlimit = IntegerField(default=DEFAULT_KDF_MEMLIMIT)

    # Bumped on "reset keys" (new keypair, history unreadable). Conversation
    # keys record which bundle version they were sealed to.
    version = PositiveIntegerField(default=1)

    objects = MessagingKeyBundleManager()

    if TYPE_CHECKING:
        profile_id: int

    def __str__(self) -> str:
        """Return a human-readable description of this bundle.

        Returns:
            String like "MessagingKeyBundle(profile=3, v1)".
        """
        return f"MessagingKeyBundle(profile={self.profile_id}, v{self.version})"

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_e2ee_key_bundle"
