"""Postgres login roles, one per process tier, each with its own connection budget.

Each tier logs in as ``ul_<UL_PROCESS_ROLE>``, inheriting table privileges from one group role, with a
``CONNECTION LIMIT`` and a deadline. The limits sum to no more than the server accepts from non-superusers, so a
tier that exhausts its budget fails its own connections rather than everyone's, and the owner - a superuser -
keeps ``superuser_reserved_connections`` for migrations and operators. See R29 (``docs/notes/database-roles.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db import connections, transaction
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.utils import CursorWrapper

logger = logging.getLogger(__name__)

#: The group every login role inherits its table privileges from.
GROUP_ROLE = "urbanlens_app"

#: nginx's ``proxy_read_timeout`` for the app; work that outlives it has nobody left to answer.
REQUEST_DEADLINE_SECONDS = 120

#: The predefined roles a tier may be granted. Anything else is refused, and anything held but undeclared is revoked.
GRANTABLE_PREDEFINED_ROLES = frozenset({"pg_read_all_stats", "pg_read_all_data"})

#: Environments where the tiers must not share the owner's password.
SEPARATE_PASSWORD_ENVIRONMENTS = frozenset({"production", "staging"})

_DEADLINE_SETTINGS = ("statement_timeout", "idle_in_transaction_session_timeout")


class DatabaseRoleError(RuntimeError):
    """The declared roles cannot be applied as they stand."""


class ConnectionBudgetExceededError(DatabaseRoleError):
    """The role limits exceed the connections the server accepts from non-superusers."""


class SharedDatabasePasswordError(DatabaseRoleError):
    """A deployed environment would hand every tier the owner's password."""


@dataclass(frozen=True, slots=True)
class DatabaseRole:
    """One process tier's login role.

    Attributes:
        process_role: The ``UL_PROCESS_ROLE`` of every container that logs in as this role.
        connection_limit: Its ``CONNECTION LIMIT``.
        deadline_seconds: Its ``statement_timeout`` and ``idle_in_transaction_session_timeout``.
        predefined_roles: Postgres predefined roles granted on top of the group's table privileges.
    """

    process_role: str
    connection_limit: int
    deadline_seconds: int
    predefined_roles: frozenset[str] = frozenset()

    @property
    def name(self) -> str:
        """The login role's name.

        Returns:
            ``ul_`` followed by the process role.
        """
        return f"ul_{self.process_role}"


@dataclass(frozen=True, slots=True)
class AppliedRole:
    """What applying one role did."""

    role: DatabaseRole
    created: bool


def declared_roles() -> tuple[DatabaseRole, ...]:
    """Every tier's login role, sized to the concurrency ``docker-compose.yml`` and ``package.json`` give it.

    A Celery tier's limit covers each container's pool plus its parent process. ``test_connection_budget_wiring``
    fails when a tier's configured concurrency outgrows its limit.

    Returns:
        The roles, in the order the command reports them.
    """
    task_deadline = int(settings.CELERY_TASK_TIME_LIMIT)
    reads_stats = frozenset({"pg_read_all_stats"})
    return (
        # 3 gunicorn workers x 4 threads, each keeping its connection; the rest is headroom for timeout_utils' executor and more workers.
        DatabaseRole("web", 54, REQUEST_DEADLINE_SECONDS, reads_stats),
        # Channels runs every consumer's database call on one thread; the health probe is the other.
        DatabaseRole("websocket", 3, REQUEST_DEADLINE_SECONDS, reads_stats),
        DatabaseRole("worker", 5, task_deadline),
        # The backup's pg_dump is a connection of its own, and reads schemas the app never touches.
        DatabaseRole("bulk", 4, task_deadline, frozenset({"pg_read_all_data"})),
        DatabaseRole("panels", 21, task_deadline),
        DatabaseRole("sandbox", 5, task_deadline),
        DatabaseRole("ai", 3, task_deadline),
        DatabaseRole("beat", 1, REQUEST_DEADLINE_SECONDS),
        DatabaseRole("metrics", 1, REQUEST_DEADLINE_SECONDS),
    )


def app_role_password(owner_password: str) -> str:
    """The password every login role uses.

    Args:
        owner_password: ``UL_DB_PASS``, the owner's.

    Returns:
        ``UL_DB_APP_PASS``, or outside staging and production the owner's password when that is unset.

    Raises:
        SharedDatabasePasswordError: In staging or production, ``UL_DB_APP_PASS`` is unset or equals the owner's.
    """
    configured = settings.UL_DB_APP_PASS
    if configured and configured != owner_password:
        return configured
    if settings.ENVIRONMENT_NAME in SEPARATE_PASSWORD_ENVIRONMENTS:
        raise SharedDatabasePasswordError(
            f"UL_DB_APP_PASS must be set, and differ from UL_DB_PASS, in {settings.ENVIRONMENT_NAME}: otherwise every tier, the sandbox included, holds the superuser's password.",
        )
    logger.warning("UL_DB_APP_PASS is unset or matches UL_DB_PASS, so the tiers' login roles share the owner's password.")
    return owner_password


def apply_database_roles(roles: Sequence[DatabaseRole], password: str, *, using: str = "default") -> list[AppliedRole]:
    """Create or converge the group role, every login role, and their privileges, in one transaction.

    Must run as a superuser: only one may grant the predefined roles. Concurrent runs against one database wait for
    each other; roles are cluster-wide, but the lock is not, so runs against two databases on one server may collide.

    Args:
        roles: The login roles to converge on, normally ``declared_roles()``.
        password: Their password, normally ``app_role_password()``. Only its SCRAM verifier reaches the server.
        using: The database alias to connect through.

    Returns:
        One entry per role, in the order given.

    Raises:
        ConnectionBudgetExceededError: The limits exceed what the server accepts from non-superusers.
        DatabaseRoleError: A role names a predefined role outside ``GRANTABLE_PREDEFINED_ROLES``.
    """
    for role in roles:
        unknown = role.predefined_roles - GRANTABLE_PREDEFINED_ROLES
        if unknown:
            raise DatabaseRoleError(f"{role.name} names predefined roles outside GRANTABLE_PREDEFINED_ROLES: {sorted(unknown)}")

    connection = connections[using]
    with transaction.atomic(using=using), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext('urbanlens.database_roles'))")
        _check_budget(cursor, roles)
        _converge_group(cursor)
        applied = [_converge_login_role(cursor, role, _password_verifier(connection, password, role.name)) for role in roles]
        _retire_undeclared(cursor, {role.name for role in roles})
    return applied


def _password_verifier(connection: BaseDatabaseWrapper, password: str, role_name: str) -> str:
    """The verifier the server will store for *password*, computed in this process.

    ``PQencryptPasswordConn`` reads the server's own ``password_encryption`` over the connection
    and produces the verifier that setting asks for, so the plaintext is never sent. psycopg3
    exposes it on the connection's ``pgconn`` rather than as a module function.

    Args:
        connection: The Django connection the roles are being converged through.
        password: The plaintext, which does not leave this process.
        role_name: The role the verifier is salted for.

    Returns:
        The verifier, as ``ALTER ROLE ... PASSWORD`` wants it.
    """
    return connection.connection.pgconn.encrypt_password(password.encode(), role_name.encode()).decode()


def _scalar(cursor: CursorWrapper, query: str, params: Sequence[str] | None = None) -> Any:
    cursor.execute(query, params)
    row = cursor.fetchone()
    return None if row is None else row[0]


def _check_budget(cursor: CursorWrapper, roles: Sequence[DatabaseRole]) -> None:
    usable = int(
        _scalar(
            cursor,
            "SELECT current_setting('max_connections')::int - current_setting('superuser_reserved_connections')::int - coalesce(current_setting('reserved_connections', true)::int, 0)",
        ),
    )
    limits = sum(role.connection_limit for role in roles)
    if limits > usable:
        raise ConnectionBudgetExceededError(f"Role connection limits sum to {limits}, over the {usable} connections this server accepts from non-superusers.")


def _converge_group(cursor: CursorWrapper) -> None:
    group = sql.Identifier(GROUP_ROLE)
    if _scalar(cursor, "SELECT 1 FROM pg_roles WHERE rolname = %s", [GROUP_ROLE]) is None:
        cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(group))
    database = sql.Identifier(_scalar(cursor, "SELECT current_database()"))
    for statement in (
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
        "GRANT CONNECT, TEMPORARY ON DATABASE {database} TO {group}",
        "GRANT USAGE ON SCHEMA public TO {group}",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {group}",
        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {group}",
        # For the tables later migrations create, since the owner running this also runs those.
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {group}",
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {group}",
    ):
        cursor.execute(sql.SQL(statement).format(database=database, group=group))


def _converge_login_role(cursor: CursorWrapper, role: DatabaseRole, password_verifier: str) -> AppliedRole:
    name = sql.Identifier(role.name)
    created = _scalar(cursor, "SELECT 1 FROM pg_roles WHERE rolname = %s", [role.name]) is None
    if created:
        cursor.execute(sql.SQL("CREATE ROLE {}").format(name))
    cursor.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {} PASSWORD {}").format(
            name,
            sql.Literal(role.connection_limit),
            sql.Literal(password_verifier),
        ),
    )
    cursor.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(GROUP_ROLE), name))
    for setting in _DEADLINE_SETTINGS:
        cursor.execute(sql.SQL("ALTER ROLE {} SET {} = {}").format(name, sql.Identifier(setting), sql.Literal(f"{role.deadline_seconds}s")))

    cursor.execute(
        "SELECT granted.rolname FROM pg_auth_members m JOIN pg_roles granted ON granted.oid = m.roleid JOIN pg_roles member ON member.oid = m.member WHERE member.rolname = %s AND granted.rolname = ANY(%s)",
        [role.name, sorted(GRANTABLE_PREDEFINED_ROLES)],
    )
    held = {row[0] for row in cursor.fetchall()}
    for predefined in sorted(role.predefined_roles - held):
        cursor.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(predefined), name))
    for predefined in sorted(held - role.predefined_roles):
        cursor.execute(sql.SQL("REVOKE {} FROM {}").format(sql.Identifier(predefined), name))
    return AppliedRole(role=role, created=created)


def _retire_undeclared(cursor: CursorWrapper, declared: set[str]) -> None:
    """Revoke login from a group member no longer declared; its open sessions end on their own."""
    cursor.execute(
        "SELECT member.rolname FROM pg_auth_members m JOIN pg_roles grp ON grp.oid = m.roleid JOIN pg_roles member ON member.oid = m.member WHERE grp.rolname = %s AND member.rolcanlogin",
        [GROUP_ROLE],
    )
    for (name,) in cursor.fetchall():
        if name not in declared:
            logger.warning("Login role %s is no longer declared; revoking its login.", name)
            cursor.execute(sql.SQL("ALTER ROLE {} WITH NOLOGIN").format(sql.Identifier(name)))
