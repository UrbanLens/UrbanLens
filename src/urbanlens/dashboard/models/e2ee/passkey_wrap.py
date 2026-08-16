"""Passkey-wrapped copies of the E2EE identity private key.

One row per (passkey, bundle): the identity private key wrapped under a secret
derived from the WebAuthn ``prf`` extension's output for that credential. The
PRF secret lives in the user's authenticator (or their passkey-sync fabric) -
never on this server, never in browser storage - so this is a *convenience*
unlock path with the same trust shape as the password wrap: the server stores
a blob it cannot open. See ``docs/designs/e2ee-passkey-unlock.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import CASCADE, CharField, ForeignKey, OneToOneField, PositiveIntegerField, TextField

from urbanlens.dashboard.models import abstract
from urbanlens.dashboard.models.e2ee.queryset import E2EEPasskeyWrapManager


class E2EEPasskeyWrap(abstract.DashboardModel):
    """One passkey's wrapped copy of a profile's identity private key.

    ``wrapped_secret`` is a ``crypto_secretbox`` blob (base64: nonce ||
    ciphertext) of the private key under ``HKDF-SHA256(prf_output,
    info="urbanlens-e2ee-passkey-wrap-v1")``, computed entirely client-side.
    ``prf_input`` is the random 32-byte PRF evaluation input minted for this
    wrap - not secret (a salt-alike), but required to re-derive the same PRF
    output at unlock time.

    ``bundle_version`` pins the wrap to the keypair it encrypts:
    ``E2EEResetView`` deletes all wraps inside its atomic swap, and the
    version stamp is the belt-and-braces backstop should a wrap ever survive
    one - the keys endpoint serves only wraps matching the bundle's current
    version.
    """

    credential = OneToOneField("dashboard.WebAuthnCredential", on_delete=CASCADE, related_name="e2ee_wrap")
    bundle = ForeignKey("dashboard.MessagingKeyBundle", on_delete=CASCADE, related_name="passkey_wraps")
    # Base64 32-byte PRF evaluation input, unique per wrap.
    prf_input = CharField(max_length=64)
    # crypto_secretbox blob (base64: nonce || ciphertext) of the private key,
    # wrapped under the HKDF of this credential's PRF output.
    wrapped_secret = TextField()
    # MessagingKeyBundle.version this wrap's plaintext belongs to.
    bundle_version = PositiveIntegerField()

    objects = E2EEPasskeyWrapManager()

    if TYPE_CHECKING:
        credential_id: int
        bundle_id: int

    class Meta(abstract.DashboardModel.Meta):
        db_table = "dashboard_e2ee_passkey_wrap"

    def __str__(self) -> str:
        """Return a human-readable description of this wrap.

        Returns:
            String like "E2EEPasskeyWrap(credential=3, bundle=7 v2)".
        """
        return f"E2EEPasskeyWrap(credential={self.credential_id}, bundle={self.bundle_id} v{self.bundle_version})"
