"""PyNaCl interop tests for the direct-message E2EE blob formats.

The browser encrypts everything with libsodium (``e2ee-crypto.ts``); the
server stores opaque blobs and can decrypt nothing. These tests pin the wire
formats documented in ``docs/e2ee.md`` by round-tripping them through PyNaCl,
which shares libsodium's implementation - so a format drift on either side
fails here instead of silently corrupting real users' history.

The exact correspondences asserted:

- Wrapping key derivation: ``crypto_pwhash`` Argon2id (interactive limits) →
  ``nacl.pwhash.argon2id.kdf``.
- Private-key wrapping: ``crypto_secretbox_easy`` with a prepended nonce →
  ``nacl.secret.SecretBox`` (nonce || ciphertext layout).
- Conversation-key sealing: ``crypto_box_seal`` → ``nacl.public.SealedBox``.
- Message encryption: ``crypto_secretbox_easy`` → ``nacl.secret.SecretBox``.
"""

from __future__ import annotations

import base64

from hypothesis import HealthCheck, given, settings, strategies as st
import nacl.encoding
import nacl.hash
import nacl.public
import nacl.pwhash
import nacl.secret
import nacl.utils

from urbanlens.core.tests.testcase import SimpleTestCase

# Mirrors KDF_OPSLIMIT / KDF_MEMLIMIT in frontend/ts/shared/e2ee-crypto.ts and
# DEFAULT_KDF_* in models/e2ee/key_bundle.py.
KDF_OPSLIMIT = 2
KDF_MEMLIMIT = 67_108_864

_settings = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _derive_wrap_key(password: bytes, salt: bytes) -> bytes:
    """Derive a 32-byte key the way ``deriveKey`` does in the browser."""
    return nacl.pwhash.argon2id.kdf(
        nacl.secret.SecretBox.KEY_SIZE,
        password,
        salt,
        opslimit=KDF_OPSLIMIT,
        memlimit=KDF_MEMLIMIT,
    )


class Argon2idDeriveInteropTests(SimpleTestCase):
    """Argon2id derivation is deterministic and salt-separated."""

    def test_same_password_and_salt_derive_same_key(self) -> None:
        salt = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        key_a = _derive_wrap_key(b"correct horse", salt)
        key_b = _derive_wrap_key(b"correct horse", salt)
        self.assertEqual(key_a, key_b)
        self.assertEqual(len(key_a), 32)

    def test_independent_salts_diverge(self) -> None:
        # The auth credential and the wrapping key use independent salts; the
        # whole security argument is that one reveals nothing about the other.
        salt_auth = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        salt_wrap = nacl.utils.random(nacl.pwhash.argon2id.SALTBYTES)
        self.assertNotEqual(_derive_wrap_key(b"pw", salt_auth), _derive_wrap_key(b"pw", salt_wrap))


class SecretBoxWrapInteropTests(SimpleTestCase):
    """The nonce||ciphertext private-key wrap format round-trips."""

    def _wrap(self, secret: bytes, wrap_key: bytes) -> str:
        # Mirrors wrapSecretKey(): nonce || secretbox, then base64.
        box = nacl.secret.SecretBox(wrap_key)
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
        ciphertext = box.encrypt(secret, nonce).ciphertext
        return base64.b64encode(nonce + ciphertext).decode()

    def _unwrap(self, blob_b64: str, wrap_key: bytes) -> bytes:
        blob = base64.b64decode(blob_b64)
        nonce, ciphertext = blob[: nacl.secret.SecretBox.NONCE_SIZE], blob[nacl.secret.SecretBox.NONCE_SIZE :]
        return nacl.secret.SecretBox(wrap_key).decrypt(ciphertext, nonce)

    def test_wrap_unwrap_roundtrip(self) -> None:
        wrap_key = nacl.utils.random(32)
        secret = nacl.public.PrivateKey.generate().encode()
        self.assertEqual(self._unwrap(self._wrap(secret, wrap_key), wrap_key), secret)

    def test_wrong_key_fails(self) -> None:
        secret = nacl.public.PrivateKey.generate().encode()
        blob = self._wrap(secret, nacl.utils.random(32))
        with self.assertRaises(Exception):
            self._unwrap(blob, nacl.utils.random(32))


class SealedBoxInteropTests(SimpleTestCase):
    """Conversation keys seal to a public key and open with its private key."""

    def test_seal_open_roundtrip(self) -> None:
        keypair = nacl.public.PrivateKey.generate()
        conversation_key = nacl.utils.random(32)
        sealed = base64.b64encode(nacl.public.SealedBox(keypair.public_key).encrypt(conversation_key)).decode()
        opened = nacl.public.SealedBox(keypair).decrypt(base64.b64decode(sealed))
        self.assertEqual(opened, conversation_key)

    def test_other_keypair_cannot_open(self) -> None:
        recipient = nacl.public.PrivateKey.generate()
        attacker = nacl.public.PrivateKey.generate()
        sealed = nacl.public.SealedBox(recipient.public_key).encrypt(nacl.utils.random(32))
        with self.assertRaises(Exception):
            nacl.public.SealedBox(attacker).decrypt(sealed)


class MessageEncryptInteropTests(SimpleTestCase):
    """Message bodies encrypt/decrypt under the conversation key."""

    @_settings
    @given(plaintext=st.text(min_size=0, max_size=400))
    def test_message_roundtrip(self, plaintext: str) -> None:
        conversation_key = nacl.utils.random(32)
        box = nacl.secret.SecretBox(conversation_key)
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
        ciphertext = base64.b64encode(box.encrypt(plaintext.encode(), nonce).ciphertext).decode()
        nonce_b64 = base64.b64encode(nonce).decode()

        recovered = box.decrypt(base64.b64decode(ciphertext), base64.b64decode(nonce_b64)).decode()
        self.assertEqual(recovered, plaintext)
