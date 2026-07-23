"""Custom model fields shared across the dashboard app."""

from __future__ import annotations

import base64
from functools import lru_cache
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings as django_settings
from django.db.models import TextField

from urbanlens.UrbanLens.settings.app import settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    """Return the process-wide Fernet instance used by ``EncryptedTextField``.

    Uses ``settings.field_encryption_key`` when set; otherwise derives a stable
    key from Django's ``SECRET_KEY`` (not ``AppSettings.secret_key`` - that
    pydantic field has no wired env var in any deployment of this app, so it
    silently falls back to a fresh random value in *every* process, which
    made encrypted fields undecryptable across gunicorn workers/Celery/manage.py
    runs the moment ``field_encryption_key`` was left unset). ``SECRET_KEY`` is
    read from ``DJANGO_SECRET_KEY``, which every deployment already sets
    consistently, so this key is stable process-to-process without requiring
    a new secret. Cached because Fernet key setup is not free and this is
    called on every encrypted field read/write.

    Returns:
        A ``Fernet`` instance ready for ``encrypt``/``decrypt``.
    """
    raw_key = settings.field_encryption_key or django_settings.SECRET_KEY
    derived = hashlib.sha256(raw_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


class EncryptedTextField(TextField):
    """A ``TextField`` whose value is encrypted at rest with Fernet.

    Plaintext only ever exists in memory - the database stores ciphertext. A
    row written with a different key (e.g. after ``UL_FIELD_ENCRYPTION_KEY``
    changes without migrating old rows) fails loudly with ``InvalidToken``
    rather than silently returning garbage or dropping the credential.
    """

    def get_prep_value(self, value: object) -> str | None:
        """Encrypt ``value`` for storage.

        Args:
            value: The plaintext string to encrypt, or None/empty.

        Returns:
            The ciphertext to store, or the original falsy value unchanged.
        """
        prepped = super().get_prep_value(value)
        if not prepped:
            return prepped
        return _fernet().encrypt(str(prepped).encode()).decode()

    def from_db_value(self, value: str | None, expression: object, connection: object) -> str | None:
        """Decrypt a stored value read from the database.

        Args:
            value: The ciphertext read from the database, or None/empty.
            expression: Unused (required by Django's field API).
            connection: Unused (required by Django's field API).

        Returns:
            The decrypted plaintext, or the original falsy value unchanged.

        Raises:
            InvalidToken: When the stored ciphertext cannot be decrypted with
                the current key.
        """
        if not value:
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            raise InvalidToken(f"Could not decrypt {self.model.__name__}.{self.name} - field_encryption_key may have changed.") from None
