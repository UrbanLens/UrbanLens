"""Integer parameters a column can't hold fail, as the server fails them, instead of being stored as their low bits.

Django sends an ``IntegerField``'s value as psycopg's ``Int4`` (``Int2`` for a small one, ``Int8`` for a big one),
and with server-side binding it goes to Postgres in binary. psycopg-binary's C dumpers for ``Int2`` and ``Int4`` keep
the low 16 or 32 bits without checking: 2**31 was stored as -2**31, 2**32 + 1 as 1. Any write that skipped a form's
range validators - a serializer field without ``max_value``, a view's ``int()`` - stored the wrong number silently.
The ``Int8`` one raises ``OverflowError`` past 2**63, which no caller expects from a write.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg import DataError
from psycopg.types.numeric import Int2, Int2BinaryDumper, Int4, Int4BinaryDumper, Int8, Int8BinaryDumper

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper
    from psycopg.abc import Buffer
    from psycopg.adapt import AdaptersMap


def _checked(value: int, bits: int, type_name: str) -> int:
    if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
        # The server's own wording for the same mistake in a text parameter.
        raise DataError(f'value "{value}" is out of range for type {type_name}')
    return value


class CheckedInt2BinaryDumper(Int2BinaryDumper):
    """psycopg's pure-Python ``smallint`` dumper, refusing what it cannot represent."""

    def dump(self, obj: int) -> Buffer | None:
        return super().dump(_checked(obj, 16, "smallint"))


class CheckedInt4BinaryDumper(Int4BinaryDumper):
    """psycopg's pure-Python ``integer`` dumper, refusing what it cannot represent."""

    def dump(self, obj: int) -> Buffer | None:
        return super().dump(_checked(obj, 32, "integer"))


class CheckedInt8BinaryDumper(Int8BinaryDumper):
    """psycopg's pure-Python ``bigint`` dumper, refusing what it cannot represent."""

    def dump(self, obj: int) -> Buffer | None:
        return super().dump(_checked(obj, 64, "bigint"))


def register_checked_integer_dumpers(adapters: AdaptersMap) -> None:
    """Dump ``Int2``/``Int4``/``Int8`` parameters through the range-checking dumpers.

    Args:
        adapters: A connection's adapters map.
    """
    adapters.register_dumper(Int2, CheckedInt2BinaryDumper)
    adapters.register_dumper(Int4, CheckedInt4BinaryDumper)
    adapters.register_dumper(Int8, CheckedInt8BinaryDumper)


def on_connection_created(sender: object, connection: BaseDatabaseWrapper, **kwargs: object) -> None:
    """``connection_created`` receiver: every new Postgres connection gets the checked dumpers.

    Args:
        sender: The backend's wrapper class.
        connection: The wrapper whose connection was just opened.
        **kwargs: The signal's remaining arguments.
    """
    if connection.vendor == "postgresql":
        register_checked_integer_dumpers(connection.connection.adapters)


def install_checked_integer_dumpers() -> None:
    """Check every connection from now on, and any that is already open."""
    from django.db import connections
    from django.db.backends.signals import connection_created

    connection_created.connect(on_connection_created, dispatch_uid="urbanlens.checked_integer_dumpers")
    for connection in connections.all(initialized_only=True):
        if connection.connection is not None:
            on_connection_created(type(connection), connection)
