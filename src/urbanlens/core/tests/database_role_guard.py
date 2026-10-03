"""Stop a test run whose database role cannot create the test database, and say where tests run instead."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from django.db import DatabaseError


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[Any]: ...


def refusal_for_role(connection: _Connection) -> str | None:
    """Why this connection's role cannot run the suite, or None when it can.

    The app containers log in as their own per-tier role, which cannot create databases (D11), so pytest there
    would otherwise fail several layers down in ``CREATE DATABASE``.

    Args:
        connection: A connection to the configured (not yet the test) database.

    Returns:
        A message naming the role and ``bin/run_tests.sh``, or None when the role may create databases or the
        database could not be asked, so that a real connection failure surfaces as it would have.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user, rolcreatedb OR rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cursor.fetchone()
    except DatabaseError:
        return None
    if row is None or row[1]:
        return None
    return (
        f"The database role {row[0]!r} cannot create a test database: the app containers log in as their own "
        "per-tier role, which is kept from creating databases (D11, docs/notes/database-roles.md). "
        "Run tests in the test-runner container with bin/run_tests.sh."
    )
