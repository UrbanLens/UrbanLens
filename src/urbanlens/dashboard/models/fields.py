"""Custom model fields shared across the dashboard app."""

from __future__ import annotations

import base64
from functools import lru_cache
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Self

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

    ``MultiFernet`` encrypts with the *first* key and decrypts with the *first that works*,
    so writes use the active key while reads still understand retired ones. Django's
    ``SECRET_KEY`` is always last so pre-existing rows stay readable.

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

    Uses ``settings.field_encryption_key`` when set, else Django's ``SECRET_KEY`` (stable
    across processes, unlike the unset pydantic field). Cached since key setup runs on
    every encrypted read/write.

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


class UndecryptableValue(str):
    """A ``fail_soft`` read that could not be decrypted, carrying its ciphertext.

    Subclasses ``str`` so consumers see the field default while the original ciphertext rides
    along in :attr:`ciphertext`, letting :meth:`EncryptedTextField.get_prep_value` write it
    back unchanged instead of overwriting recoverable data with the default on the next save.

    Only covers string defaults; prefer ``blank=True, default=""`` over ``null=True`` for new
    ``fail_soft`` content fields.
    """

    __slots__ = ("ciphertext",)

    #: The undecryptable ciphertext exactly as read from the database.
    ciphertext: str

    def __new__(cls, default: str, ciphertext: str) -> Self:
        """Build a default-valued string that remembers its ciphertext.

        Args:
            default: The field default to present to callers.
            ciphertext: The raw stored value that could not be decrypted.

        Returns:
            A ``str`` equal to the field default, tagged with the ciphertext.
        """
        instance = super().__new__(cls, default)
        instance.ciphertext = ciphertext
        return instance

    def __reduce__(self) -> tuple[type[Self], tuple[str, str]]:
        """Support pickling/copying, which cached model instances hit.

        ``str``'s default reduction calls ``cls(one_argument)``, which fails against this
        two-argument ``__new__``.

        Returns:
            The callable and arguments needed to rebuild an equivalent value.
        """
        return (type(self), (str(self), self.ciphertext))


class EncryptedTextField(TextField):
    """A ``TextField`` whose value is encrypted at rest with Fernet.

    Values are written under the active key and read under any key in :func:`encryption_keys`;
    rotate via ``docs/DATA_ENCRYPTION.md``. An undecryptable value depends on ``fail_soft``:

    - **Credentials** leave ``fail_soft`` off and raise: callers drop the row and the user
      reconnects, since the value is re-obtainable from the provider.
    - **User-authored content** sets ``fail_soft=True``: there is no external copy, so the row
      degrades to its default rather than raising (see :class:`UndecryptableValue`).

    Declare new ``fail_soft`` content fields as ``blank=True, default=""`` rather than
    ``null=True`` so a later ``save()`` preserves the ciphertext.
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
        # Undecryptable values go back as they came, never re-encrypted: their carried default
        # is usually "" and would otherwise fall through the falsy guard below.
        if isinstance(value, UndecryptableValue):
            return value.ciphertext
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
                # error, not exception: one bad row would otherwise log a traceback per field
                # per request; the message already names the field and setting to check.
                logger.error(  # noqa: TRY400
                    "Could not decrypt %s.%s - no configured key matches. Reading as empty; the row is left intact so it is still recoverable if the original key is restored. Check field_encryption_key/field_encryption_key_fallbacks.",
                    model_name,
                    self.name,
                )
                # The ciphertext travels with the default so a later save writes the original bytes
                # back (see UndecryptableValue); nullable fields still degrade to plain None.
                default = self.get_default()
                if isinstance(default, str):
                    return UndecryptableValue(default, value)
                return default
            raise InvalidToken(f"Could not decrypt {model_name}.{self.name} - field_encryption_key may have changed.") from None


class UndecryptableJSON(dict):
    """A ``fail_soft`` JSON read that could not be decrypted, carrying its ciphertext.

    The mapping counterpart to :class:`UndecryptableValue` for nullable JSON fields: an empty
    ``dict`` reads like "no data recorded" while preserving the ciphertext for a later save.

    Args:
        ciphertext: The undecryptable value exactly as read from the database.
    """

    __slots__ = ("ciphertext",)

    def __init__(self, ciphertext: str) -> None:
        super().__init__()
        self.ciphertext = ciphertext


#: Anything ``json.dumps`` round-trips. Named so the descriptor declarations
#: below read as intent rather than as a widening.
type JSONValue = dict[str, Any] | list[Any] | str | int | float | bool | None


class EncryptedJSONField(EncryptedTextField):
    """A JSON field whose serialised value is encrypted at rest with Fernet.

    Stored as ``text``: ciphertext is opaque so the database could not query inside it anyway.
    Subclasses :class:`EncryptedTextField` so rotation picks these columns up by ``isinstance``.

    Reads give back whatever was stored (usually a ``dict``), ``None`` for an empty column, or
    an empty :class:`UndecryptableJSON` when ``fail_soft`` swallowed a failure.
    """

    if TYPE_CHECKING:
        # The storage type is text but the Python type is a JSON value; declaring so keeps
        # callers from working around a `str` type the attribute never has.
        def __get__(self, instance: Any, owner: Any) -> JSONValue: ...

        def __set__(self, instance: Any, value: JSONValue) -> None: ...

    def get_prep_value(self, value: object) -> str | None:
        """Serialise then encrypt ``value`` for storage.

        Args:
            value: The JSON-serialisable value, or None.

        Returns:
            The ciphertext to store, or None.
        """
        # Same pass-through contract as UndecryptableValue, for the nullable case.
        if isinstance(value, UndecryptableJSON):
            return value.ciphertext
        if value is None:
            return None
        return super().get_prep_value(json.dumps(value, separators=(",", ":"), sort_keys=True))

    def from_db_value(self, value: str | None, expression: object, connection: object) -> object:
        """Decrypt and deserialise a stored value.

        Args:
            value: The ciphertext read from the database, or None/empty.
            expression: Unused (required by Django's field API).
            connection: Unused (required by Django's field API).

        Returns:
            The stored object, None for an empty column, or an empty
            :class:`UndecryptableJSON` when ``fail_soft`` swallowed a failure.

        Raises:
            InvalidToken: No configured key could decrypt it and ``fail_soft``
                is not set.
        """
        decrypted = super().from_db_value(value, expression, connection)
        if not decrypted:
            # A non-empty column reading empty is the fail_soft path; recover the ciphertext so a
            # save before the key is fixed writes the original bytes back.
            return UndecryptableJSON(value) if value else None
        return json.loads(decrypted)
