"""Reading a client-supplied UUID, which a ``UUIDField`` lookup refuses with a ``ValidationError`` - a 500 - when malformed."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Iterable


def uuid_or_none(value: object) -> UUID | None:
    """*value* as a UUID, or None when it is not one.

    Args:
        value: A request parameter, a decoded JSON value, or a UUID.

    Returns:
        The UUID, or None for anything a ``UUIDField`` would refuse.
    """
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def valid_uuids(values: Iterable[object]) -> list[UUID]:
    """The UUIDs among *values*, dropping anything that is not one.

    Args:
        values: Request parameters or decoded JSON values.

    Returns:
        Each one that is a UUID, in order.
    """
    return [parsed for parsed in map(uuid_or_none, values) if parsed is not None]
