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

MAX_PIN_DESCRIPTION_LENGTH = 50_000
MAX_WIKI_DESCRIPTION_LENGTH = 50_000
MAX_PIN_LIST_DESCRIPTION_LENGTH = 50_000
MAX_TRIP_DESCRIPTION_LENGTH = 50_000
MAX_TRIP_ACTIVITY_NOTES_LENGTH = 50_000
MAX_PROFILE_BIO_LENGTH = 50_000
MAX_COMMENT_TEXT_LENGTH = 1_000
MAX_MARKUP_LABEL_LENGTH = 500
MAX_PIN_SHARE_MESSAGE_LENGTH = 5_000
MAX_DIRECT_MESSAGE_LENGTH = 1_000
MAX_FRIEND_REQUEST_MESSAGE_LENGTH = 1_000


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
