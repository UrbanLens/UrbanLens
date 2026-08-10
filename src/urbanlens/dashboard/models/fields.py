"""Custom model fields shared across the dashboard app."""

from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings as django_settings
from django.db.models import TextField

from urbanlens.UrbanLens.settings.app import settings

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def _derive_fernet(raw_key: str) -> Fernet:
    """Build a ``Fernet`` from an arbitrary-length secret string.

    Args:
        raw_key: The raw secret to derive a Fernet key from.

    Returns:
        A ``Fernet`` keyed on the SHA256 digest of ``raw_key``.
    """
    derived = hashlib.sha256(raw_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encryption_keys() -> list[str]:
    """Return every key to try, active key first.

    Order is what makes a key change survivable: ``MultiFernet`` encrypts with
    the *first* key and decrypts with the *first that works*, so writes always
    use the active key while reads still understand anything written under a
    retired one.

    Django's ``SECRET_KEY`` is always appended as a last resort. Every install
    that predates ``field_encryption_key`` being set has its data encrypted
    under it (see ``_fernet``'s history below), so without this, the act of
    setting ``UL_FIELD_ENCRYPTION_KEY`` for the first time would itself orphan
    every existing row - the exact footgun this list exists to prevent. Add the
    *old* ``SECRET_KEY`` to ``field_encryption_key_fallbacks`` when rotating
    ``SECRET_KEY`` itself, since only the current one is implicit here.

    Returns:
        Deduplicated, non-empty keys in decryption-attempt order.
    """
    candidates = [
        settings.field_encryption_key or django_settings.SECRET_KEY,
        *settings.field_encryption_key_fallbacks,
        django_settings.SECRET_KEY,
    ]
    ordered: list[str] = []
    for key in candidates:
        if key and key not in ordered:
            ordered.append(key)
    return ordered


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    """Return the process-wide ``MultiFernet`` used by ``EncryptedTextField``.

    Uses ``settings.field_encryption_key`` when set; otherwise derives a stable
    key from Django's ``SECRET_KEY`` (not ``AppSettings.secret_key`` - that
    pydantic field has no wired env var in any deployment of this app, so it
    silently falls back to a fresh random value in *every* process, which
    made encrypted fields undecryptable across gunicorn workers/Celery/manage.py
    runs the moment ``field_encryption_key`` was left unset). ``SECRET_KEY`` is
    read from ``DJANGO_SECRET_KEY``, which every deployment already sets
    consistently, so this key is stable process-to-process without requiring
    a new secret. Cached because key setup is not free and this is called on
    every encrypted field read/write.

    Returns:
        A ``MultiFernet`` over :func:`encryption_keys`.
    """
    return MultiFernet([_derive_fernet(key) for key in encryption_keys()])


def reset_encryption_keys() -> None:
    """Drop the cached key set so the next call re-reads settings.

    Only needed when the configured keys change inside a live process - i.e.
    tests, and ``rotate_field_encryption`` verifying its own work.
    """
    _fernet.cache_clear()


class EncryptedTextField(TextField):
    """A ``TextField`` whose value is encrypted at rest with Fernet.

    Plaintext only ever exists in memory - the database stores ciphertext.
    Values are written under the active key and read under any key in
    :func:`encryption_keys`, so a key can be retired without downtime: add the
    new key, deploy, run ``manage.py rotate_field_encryption``, then drop the
    old key from ``field_encryption_key_fallbacks``.

    A value that no key can decrypt is unrecoverable - Fernet is authenticated
    encryption, so there is no partial read. What happens then depends on
    ``fail_soft``, and the right answer differs by what the field holds:

    - **Credentials** (OAuth tokens, API keys, TOTP seeds) leave ``fail_soft``
      off and fail loudly. Their callers already catch ``InvalidToken`` and
      drop the row so the user simply reconnects - lossless, because the value
      is re-obtainable from the provider. Silently reading ``None`` for a
      credential would instead look like a valid "not connected" state.
    - **User-authored content** (bio, private notes, contact details) sets
      ``fail_soft=True``. There is no external copy to re-fetch, so the row must
      never be auto-deleted, and raising is worse than useless: ``Profile``
      loads on nearly every authenticated request, so one bad row would take the
      whole site down for that user rather than degrading one field. The row is
      left intact so a recovered key can still restore it.
    """

    def __init__(self, *args: Any, fail_soft: bool = False, **kwargs: Any) -> None:
        """Initialise the field.

        Args:
            *args: Standard ``TextField`` positional arguments.
            fail_soft: When True, an undecryptable value reads as the field's
                default (and is logged) instead of raising ``InvalidToken``.
            **kwargs: Standard ``TextField`` keyword arguments.
        """
        self.fail_soft = fail_soft
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        """Include ``fail_soft`` in the migration representation.

        Returns:
            The standard 4-tuple, with ``fail_soft`` added when enabled.
        """
        name, path, args, kwargs = super().deconstruct()
        if self.fail_soft:
            kwargs["fail_soft"] = True
        return name, path, args, kwargs

    def get_prep_value(self, value: object) -> str | None:
        """Encrypt ``value`` for storage under the active key.

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
            The decrypted plaintext, the original falsy value unchanged, or -
            when ``fail_soft`` is set and no key could decrypt it - the field's
            default.

        Raises:
            InvalidToken: When the stored ciphertext cannot be decrypted with
                any configured key and ``fail_soft`` is not set.
        """
        if not value:
            return value
        try:
            return _fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            model_name = self.model.__name__ if hasattr(self, "model") else "<unbound>"
            if self.fail_soft:
                # logger.error, not logger.exception: this fires once per affected
                # row *per field*, so a single bad Profile would emit eight
                # identical InvalidToken tracebacks per request. The traceback adds
                # nothing here - the frames are always inside this method - and the
                # message already names the field and the setting to check.
                logger.error(  # noqa: TRY400
                    "Could not decrypt %s.%s - no configured key matches. Reading as empty; the row is left "
                    "intact so it is still recoverable if the original key is restored. Check "
                    "field_encryption_key/field_encryption_key_fallbacks.",
                    model_name,
                    self.name,
                )
                return self.get_default()
            raise InvalidToken(f"Could not decrypt {model_name}.{self.name} - field_encryption_key may have changed.") from None
