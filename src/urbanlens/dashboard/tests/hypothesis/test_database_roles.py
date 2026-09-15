"""Each tier logs in as its own capped, deadlined role that can use the schema but not change it."""

from __future__ import annotations

from contextlib import contextmanager
import dataclasses
import io
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.db import ProgrammingError, connection, transaction
from django.test import override_settings

from urbanlens.core.controllers.backups.db import DatabaseBackup
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.core.database_roles import (
    GROUP_ROLE,
    ConnectionBudgetExceededError,
    DatabaseRole,
    DatabaseRoleError,
    SharedDatabasePasswordError,
    app_role_password,
    apply_database_roles,
    declared_roles,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from django.db.backends.utils import CursorWrapper

PASSWORD = "p104-test-password"


@contextmanager
def _as_role(name: str) -> Iterator[CursorWrapper]:
    """A cursor acting as ``name`` until the block ends."""
    with connection.cursor() as cursor:
        cursor.execute(f'SET ROLE "{name}"')
        try:
            yield cursor
        finally:
            cursor.execute("RESET ROLE")


def _rows(query: str, params: Sequence[str | list[str]] | None = None) -> list[tuple]:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


def _is_member(member: str, group: str) -> bool:
    return bool(
        _rows(
            "SELECT 1 FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid JOIN pg_roles r ON r.oid = m.member"
            " WHERE r.rolname = %s AND g.rolname = %s",
            [member, group],
        ),
    )


class DeclaredRolesTests(TestCase):
    """The roles as applied, inspected from inside the test's transaction."""

    def setUp(self) -> None:
        super().setUp()
        apply_database_roles(declared_roles(), PASSWORD)

    def test_each_tier_has_its_limit_and_deadlines_and_nothing_administrative(self) -> None:
        for role in declared_roles():
            with self.subTest(role=role.name):
                attributes = _rows(
                    "SELECT rolcanlogin, rolconnlimit, rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls"
                    " FROM pg_roles WHERE rolname = %s",
                    [role.name],
                )
                self.assertEqual(attributes, [(True, role.connection_limit, False)])
                settings_rows = _rows(
                    "SELECT setconfig FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole WHERE r.rolname = %s AND s.setdatabase = 0",
                    [role.name],
                )
                self.assertEqual(len(settings_rows), 1)
                self.assertIn(f"statement_timeout={role.deadline_seconds}s", settings_rows[0][0])
                self.assertIn(f"idle_in_transaction_session_timeout={role.deadline_seconds}s", settings_rows[0][0])

    def test_only_a_scram_verifier_reaches_the_server(self) -> None:
        for role in declared_roles():
            with self.subTest(role=role.name):
                ((stored,),) = _rows("SELECT rolpassword FROM pg_authid WHERE rolname = %s", [role.name])
                self.assertTrue(
                    stored.startswith("SCRAM-SHA-256$"),
                    "a plaintext or md5 password would sit in pg_authid and the statement log",
                )

    def test_a_tier_can_use_the_schema_but_not_change_it(self) -> None:
        with _as_role("ul_sandbox") as cursor:
            cursor.execute("SELECT count(*) FROM django_migrations")
            cursor.execute("UPDATE django_migrations SET app = app WHERE id = -1")
            for statement in (
                "CREATE TABLE p104_probe (id int)",
                "DROP TABLE django_migrations",
                "TRUNCATE django_migrations",
                "CREATE ROLE p104_probe",
            ):
                with self.subTest(statement=statement), self.assertRaises(ProgrammingError), transaction.atomic():
                    cursor.execute(statement)

    def test_tables_a_later_migration_creates_are_usable_without_reapplying(self) -> None:
        """Migrations run as the owner, between one db-setup and the next."""
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE p104_later (id serial PRIMARY KEY, value int)")
        with _as_role("ul_worker") as cursor:
            cursor.execute("INSERT INTO p104_later (value) VALUES (1)")
            cursor.execute("SELECT count(*) FROM p104_later")
            self.assertEqual(cursor.fetchone(), (1,))

    def test_only_the_tiers_that_serve_the_health_probe_can_see_other_sessions(self) -> None:
        """The readiness probe counts backends by ``backend_type``, which Postgres hides from everyone else."""
        hidden = "SELECT count(*) FROM pg_stat_activity WHERE backend_type IS NULL"
        with _as_role("ul_web") as cursor:
            cursor.execute(hidden)
            self.assertEqual(cursor.fetchone(), (0,))
        with _as_role("ul_worker") as cursor:
            cursor.execute(hidden)
            self.assertNotEqual(cursor.fetchone(), (0,))

    def test_applying_again_changes_nothing(self) -> None:
        names = [role.name for role in declared_roles()]
        state = (
            "SELECT r.rolname, r.rolconnlimit, r.rolcanlogin, s.setconfig,"
            " array(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid WHERE m.member = r.oid ORDER BY 1)"
            " FROM pg_roles r LEFT JOIN pg_db_role_setting s ON s.setrole = r.oid AND s.setdatabase = 0"
            " WHERE r.rolname = ANY(%s) ORDER BY 1"
        )
        before = _rows(state, [names])

        again = apply_database_roles(declared_roles(), PASSWORD)

        self.assertEqual([entry.created for entry in again], [False] * len(names))
        self.assertEqual(_rows(state, [names]), before)

    def test_a_predefined_role_no_longer_declared_is_revoked(self) -> None:
        widened = tuple(
            dataclasses.replace(role, predefined_roles=role.predefined_roles | {"pg_read_all_data"})
            if role.process_role == "sandbox"
            else role
            for role in declared_roles()
        )
        apply_database_roles(widened, PASSWORD)
        self.assertTrue(_is_member("ul_sandbox", "pg_read_all_data"))

        apply_database_roles(declared_roles(), PASSWORD)

        self.assertFalse(_is_member("ul_sandbox", "pg_read_all_data"))

    def test_a_tier_no_longer_declared_can_no_longer_log_in(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE ROLE ul_p104_retired LOGIN IN ROLE "{GROUP_ROLE}"')

        apply_database_roles(declared_roles(), PASSWORD)

        self.assertEqual(_rows("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'ul_p104_retired'"), [(False,)])

    def test_the_command_applies_every_declared_role(self) -> None:
        out = io.StringIO()
        call_command("apply_database_roles", stdout=out)
        for role in declared_roles():
            self.assertIn(role.name, out.getvalue())


class RefusedRolesTests(TestCase):
    def test_a_budget_the_server_cannot_hold_creates_nothing(self) -> None:
        with self.assertRaises(ConnectionBudgetExceededError):
            apply_database_roles((DatabaseRole("p104_greedy", 10_000, 60),), PASSWORD)
        self.assertEqual(_rows("SELECT 1 FROM pg_roles WHERE rolname = 'ul_p104_greedy'"), [])

    def test_a_predefined_role_outside_the_allowlist_creates_nothing(self) -> None:
        """``pg_execute_server_program`` would hand a tier a shell on the database host."""
        with self.assertRaises(DatabaseRoleError):
            apply_database_roles(
                (DatabaseRole("p104_shell", 1, 60, frozenset({"pg_execute_server_program"})),), PASSWORD
            )
        self.assertEqual(_rows("SELECT 1 FROM pg_roles WHERE rolname = 'ul_p104_shell'"), [])


class AppRolePasswordTests(SimpleTestCase):
    def test_the_settings_it_reads_exist(self) -> None:
        """``override_settings`` below would otherwise invent them."""
        self.assertTrue(hasattr(settings, "UL_DB_APP_PASS"))
        self.assertTrue(hasattr(settings, "ENVIRONMENT_NAME"))

    def test_a_deployed_environment_refuses_to_share_the_owners_password(self) -> None:
        for environment in ("production", "staging"):
            for app_password in ("", "owner-secret"):
                with (
                    self.subTest(environment=environment, app_password=app_password),
                    override_settings(ENVIRONMENT_NAME=environment, UL_DB_APP_PASS=app_password),
                    self.assertRaises(SharedDatabasePasswordError),
                ):
                    app_role_password("owner-secret")

    def test_a_distinct_password_is_what_every_tier_gets(self) -> None:
        with override_settings(ENVIRONMENT_NAME="production", UL_DB_APP_PASS="tier-secret"):
            self.assertEqual(app_role_password("owner-secret"), "tier-secret")

    def test_a_checkout_without_one_shares_the_owners(self) -> None:
        with override_settings(ENVIRONMENT_NAME="development", UL_DB_APP_PASS=""):
            self.assertEqual(app_role_password("owner-secret"), "owner-secret")


class DumpsCarryNoGrantsTests(SimpleTestCase):
    def test_a_backup_restores_into_a_cluster_that_has_never_had_these_roles(self) -> None:
        """Restores go into a fresh database, where only db-setup's grants apply; a dumped GRANT to a missing role aborts one."""

        def _dump(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
            Path(cmd[cmd.index("-f") + 1]).write_bytes(b"-- dump")
            return subprocess.CompletedProcess(cmd, 0)

        with TemporaryDirectory() as tmp:
            with mock.patch.object(DatabaseBackup, "schedule_backup", return_value=False):
                backup = DatabaseBackup(auto_schedule=False)
            backup.backup_dir = Path(tmp)
            with (
                mock.patch("subprocess.run", side_effect=_dump) as run,
                mock.patch("urbanlens.core.controllers.backups.db.which", return_value="/usr/bin/pg_dump"),
                mock.patch.object(backup, "purge_old_backups"),
            ):
                self.assertTrue(backup.run())

        self.assertIn("--no-privileges", run.call_args.args[0])
