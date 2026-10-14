"""``migrate`` lets plugins bring an older database forward first, and a history this code doesn't know is refused."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile

from django.core.management import call_command
from django.db.migrations.recorder import MigrationRecorder

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.checks import check_migration_history_is_known
from urbanlens.dashboard.plugins import points
from urbanlens.dashboard.plugins.loader import TEMPLATE_OVERRIDES_DIR, PluginSource, template_override_dirs


class BeforeMigrateTests(TestCase):
    def test_migrate_fires_it_with_the_database_alias(self) -> None:
        seen: list[str] = []
        with points.BEFORE_MIGRATE.overridden([seen.append]):
            call_command("migrate", verbosity=0, interactive=False)

        self.assertEqual(seen, ["default"])

    def test_a_plan_or_a_check_changes_nothing_so_fires_nothing(self) -> None:
        seen: list[str] = []
        with points.BEFORE_MIGRATE.overridden([seen.append]):
            call_command("migrate", plan=True, verbosity=0)
            call_command("migrate", check_unapplied=True, verbosity=0)

        self.assertEqual(seen, [])


class UnknownMigrationHistoryTests(TestCase):
    def test_a_current_history_passes(self) -> None:
        self.assertEqual(check_migration_history_is_known(databases=["default"]), [])

    def test_a_recorded_migration_this_code_lacks_is_refused(self) -> None:
        MigrationRecorder.Migration.objects.create(app="dashboard", name="0999_from_another_history")

        errors = check_migration_history_is_known(databases=["default"])

        self.assertEqual([error.id for error in errors], ["dashboard.E014"])
        self.assertIn("dashboard.0999_from_another_history", errors[0].msg)

    def test_another_project_s_app_is_not_ours_to_judge(self) -> None:
        MigrationRecorder.Migration.objects.create(app="auth", name="0999_squashed_away")

        self.assertEqual(check_migration_history_is_known(databases=["default"]), [])

    def test_without_a_database_it_checks_nothing(self) -> None:
        MigrationRecorder.Migration.objects.create(app="dashboard", name="0999_from_another_history")

        self.assertEqual(check_migration_history_is_known(databases=None), [])


class TemplateOverrideDirTests(SimpleTestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name, overrides in (("ovr_with", True), ("ovr_without", False)):
            package = root / name
            package.mkdir()
            (package / "__init__.py").touch()
            if overrides:
                (package / TEMPLATE_OVERRIDES_DIR).mkdir()
        sys.path.insert(0, str(root))
        self.addCleanup(sys.path.remove, str(root))
        self.root = root

    def _source(self, name: str, *, is_package: bool = True) -> PluginSource:
        return PluginSource(name=name, module=name, origin="settings", is_package=is_package)

    def test_only_an_enabled_package_with_the_directory_contributes_one(self) -> None:
        sources = [self._source("ovr_with"), self._source("ovr_without"), self._source("ovr_module", is_package=False)]

        self.assertEqual(template_override_dirs(sources, []), [str(self.root / "ovr_with" / TEMPLATE_OVERRIDES_DIR)])
        self.assertEqual(template_override_dirs(sources, ["ovr_with"]), [])
