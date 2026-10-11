"""The project's ``TransactionTestCase`` leaves the database as Django's ``flush`` would, without truncating it."""

from __future__ import annotations

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TransactionTestCase


def _tables_with_rows() -> set[str]:
    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    with connection.cursor() as cursor:
        found = set()
        for table in tables:
            cursor.execute(f"SELECT EXISTS (SELECT 1 FROM {connection.ops.quote_name(table)})")
            if cursor.fetchone()[0]:
                found.add(table)
    return found


def _content_types() -> set[tuple[str, str]]:
    return set(ContentType.objects.values_list("app_label", "model"))


class TransactionTestCaseTeardownTests(TransactionTestCase):
    def test_every_row_the_test_wrote_is_deleted(self) -> None:
        content_types, permissions = _content_types(), Permission.objects.count()
        baker.make(User)  # a profile, its default labels and saved filters, through signals
        probe = ContentType.objects.create(app_label="teardown", model="probe")
        Permission.objects.create(content_type=probe, codename="probe", name="Probe")

        self._fixture_teardown()

        self.assertEqual(_tables_with_rows(), {"django_content_type", "auth_permission"})
        self.assertEqual(_content_types(), content_types)
        self.assertEqual(Permission.objects.count(), permissions)

    def test_a_deleted_content_type_is_put_back_with_its_permissions(self) -> None:
        content_types, permissions = _content_types(), Permission.objects.count()
        ContentType.objects.get_for_model(User).delete()

        self._fixture_teardown()

        self.assertEqual(_content_types(), content_types)
        self.assertEqual(Permission.objects.count(), permissions)
        self.assertTrue(Permission.objects.filter(content_type=ContentType.objects.get_for_model(User)).exists())

    def test_the_tables_post_migrate_fills_start_full(self) -> None:
        """Anti-vacuity: the first test's check that only these two keep rows means something only if they have any."""
        self.assertTrue(ContentType.objects.filter(app_label="auth", model="user").exists())
        self.assertTrue(Permission.objects.filter(codename="add_user").exists())
