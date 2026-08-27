"""Shared max-length constants for free-text fields, and a helper for enforcing
them on controller write paths that build/mutate models directly (bulk edit,
JSON-body endpoints, etc.) and so never run a Form/Serializer's automatic
`full_clean()`-driven `MaxLengthValidator`.

These same numbers are also set as each field's `max_length` on the model
(Django's `TextField(max_length=N)` doesn't change the DB column, but does add
a `MaxLengthValidator` that Django Forms/DRF serializers pick up automatically)
so the limit is enforced consistently regardless of which write path is used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import Model

MAX_ARTICLE_LENGTH = 200_000
MAX_ARTICLE_EDIT_SUMMARY_LENGTH = 255
MAX_PIN_DESCRIPTION_LENGTH = 50_000
MAX_WIKI_DESCRIPTION_LENGTH = 50_000
MAX_PIN_LIST_DESCRIPTION_LENGTH = 50_000
MAX_ALBUM_DESCRIPTION_LENGTH = 50_000
MAX_TRIP_DESCRIPTION_LENGTH = 50_000
MAX_TRIP_ACTIVITY_NOTES_LENGTH = 50_000
MAX_PROFILE_BIO_LENGTH = 50_000
MAX_PIN_NOTE_LENGTH = 50_000
MAX_VISIT_NOTES_LENGTH = 50_000
MAX_COMMENT_TEXT_LENGTH = 1_000
MAX_MARKUP_LABEL_LENGTH = 500
MAX_PIN_SHARE_MESSAGE_LENGTH = 5_000
MAX_DIRECT_MESSAGE_LENGTH = 1_000
#: Live session chat for SpotGuessr/Trivia/Consensus. Matches each
#: ``*SessionChatMessage.body`` field's own ``max_length`` - raising it here alone
#: would push the truncated body past what the column accepts.
MAX_SESSION_CHAT_MESSAGE_LENGTH = 1_000
MAX_FRIEND_REQUEST_MESSAGE_LENGTH = 1_000
MAX_PREFERENCE_OTHER_LENGTH = 255
MAX_ADDITIONAL_PREFERENCES_LENGTH = 1_000


def column_max_length(model: type[Model], field_name: str) -> int:
    """The declared width of `model.field_name`'s column.

    Django's Model ``_meta`` API is the documented way to ask this; the
    suppression is here, once, so callers needing a column's own width do not
    each repeat it.

    Args:
        model: The model class owning the field.
        field_name: Name of a field with a ``max_length``.

    Returns:
        The field's ``max_length``.

    Raises:
        TypeError: If the named field has no ``max_length`` - a reverse relation,
            or a column with no declared width. Callers use the result as a
            truncation bound, so returning ``None`` would silently become "no
            limit" at the call site.
    """
    field = model._meta.get_field(field_name)  # noqa: SLF001 - Model._meta is Django's documented metadata API
    max_length = getattr(field, "max_length", None)
    if not isinstance(max_length, int):
        raise TypeError(f"{model.__name__}.{field_name} has no max_length")
    return max_length


def column_length_error(model: type[Model], field_name: str, value: str | None, field_label: str) -> str | None:
    """Return an error if `value` will not fit `model.field_name`'s column.

    The constants above cover `TextField`s, whose limit exists only as a Django
    validator. This covers the other case: a `CharField` whose limit is the
    database column itself, where an over-long value is a `DataError` rather
    than a validation failure. Controllers that assign request data straight to
    such a field - names, mostly - have no validator in the way, so the check
    has to be explicit.

    The bound is read from the field so it cannot drift from the column.

    Args:
        model: The model class owning the field.
        field_name: Name of the bounded field on that model.
        value: The text to check (may be `None` or empty).
        field_label: Human-readable field name to use in the error message.

    Returns:
        An error string if `value` is too long, otherwise `None`.
    """
    return text_length_error(value, column_max_length(model, field_name), field_label)


def text_length_error(value: str | None, max_length: int, field_label: str) -> str | None:
    """Return a human-readable error if `value` exceeds `max_length`.

    Args:
        value: The text to check (may be `None` or empty).
        max_length: Maximum allowed character count.
        field_label: Human-readable field name to use in the error message.

    Returns:
        An error string if `value` is too long, otherwise `None`.
    """
    if value and len(value) > max_length:
        return f"{field_label} must be {max_length:,} characters or fewer (got {len(value):,})."
    return None
