"""Tests for the alias-deduplication step carried by migration 0008.

Production/staging databases can already have case-insensitive duplicate
aliases (e.g. "Aloha Stadium" and "aloha stadium" on the same pin) predating
the case-insensitive unique constraint that migration adds - without a
cleanup step first, `AddConstraint` fails with a real IntegrityError
("could not create unique index... is duplicated"). These tests exercise the
exact SQL the migration runs (imported directly from the migration module)
against manually-inserted duplicate rows, since the constraint the migration
adds is already active by the time any Django TestCase runs (migrations
apply once, at test-database creation) - the constraint is dropped and
duplicates are inserted via raw SQL first, both automatically undone by
TestCase's per-test transaction rollback.
"""

from __future__ import annotations

import importlib

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_migration = importlib.import_module("urbanlens.dashboard.migrations.0008_add_image_media_labels")


def _dedup_sql(table: str) -> str:
    """The exact RunSQL statement migration 0008 runs for one alias table."""
    for op in _migration.Migration.operations:
        if op.__class__.__name__ == "RunSQL" and table in op.sql:
            return op.sql
    raise AssertionError(f"No RunSQL operation found touching {table!r} - has migration 0008 changed?")


class PinAliasDedupMigrationTests(TestCase):
    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        location = baker.make(Location, latitude=41.1, longitude=-73.1)
        self.pin = baker.make(Pin, profile=self.profile, location=location)
        self.sql = _dedup_sql("dashboard_pin_aliases")

    def _insert_raw(self, name: str, kind: str = "alternate") -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO dashboard_pin_aliases (name, kind, source, pin_id, created, updated) "
                "VALUES (%s, %s, 'user', %s, now(), now())",
                [name, kind, self.pin.pk],
            )

    def _drop_constraint(self) -> None:
        with connection.cursor() as cursor:
            # setUp's baker.make(Pin, ...) auto-creates a PinAlias via Pin.save()'s
            # alias sync, leaving that insert's FK trigger event queued; Postgres
            # refuses DDL on a table with pending trigger events, so flush first.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            # A UniqueConstraint over an expression (Lower(name)) is implemented as
            # a unique INDEX, not a table CONSTRAINT - Postgres doesn't support
            # CHECK/UNIQUE table constraints on expressions, only indexes.
            cursor.execute("DROP INDEX db_pin_alias_unique")

    def test_case_insensitive_duplicates_are_collapsed_to_one_row(self) -> None:
        self._drop_constraint()
        self._insert_raw("Aloha Stadium")
        self._insert_raw("aloha stadium")
        self._insert_raw("ALOHA STADIUM")
        with connection.cursor() as cursor:
            cursor.execute(self.sql)
        self.assertEqual(PinAlias.objects.filter(pin=self.pin).count(), 1)

    def test_official_kind_is_preferred_as_the_survivor(self) -> None:
        self._drop_constraint()
        self._insert_raw("Aloha Stadium", kind="alternate")
        self._insert_raw("aloha stadium", kind="official")
        with connection.cursor() as cursor:
            cursor.execute(self.sql)
        remaining = PinAlias.objects.get(pin=self.pin)
        self.assertEqual(remaining.kind, "official")

    def test_distinct_names_are_left_alone(self) -> None:
        self._drop_constraint()
        self._insert_raw("Aloha Stadium")
        self._insert_raw("Old Mill")
        with connection.cursor() as cursor:
            cursor.execute(self.sql)
        self.assertEqual(PinAlias.objects.filter(pin=self.pin).count(), 2)

    def test_duplicates_on_different_pins_are_both_kept(self) -> None:
        self._drop_constraint()
        other_location = baker.make(Location, latitude=42.2, longitude=-74.2)
        other_pin = baker.make(Pin, profile=self.profile, location=other_location)
        self._insert_raw("Shared Name")
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO dashboard_pin_aliases (name, kind, source, pin_id, created, updated) "
                "VALUES ('shared name', 'alternate', 'user', %s, now(), now())",
                [other_pin.pk],
            )
            cursor.execute(self.sql)
        self.assertEqual(PinAlias.objects.filter(pin=self.pin).count(), 1)
        self.assertEqual(PinAlias.objects.filter(pin=other_pin).count(), 1)

    def test_constraint_can_be_recreated_after_dedup(self) -> None:
        """The actual point of the cleanup - confirms the migration's own
        AddConstraint step (case-insensitive) would succeed afterward."""
        self._drop_constraint()
        self._insert_raw("Aloha Stadium")
        self._insert_raw("aloha stadium")
        with connection.cursor() as cursor:
            cursor.execute(self.sql)
            cursor.execute(
                "CREATE UNIQUE INDEX db_pin_alias_unique ON dashboard_pin_aliases (LOWER(name), pin_id)",
            )  # raises if a duplicate survived


class WikiAliasDedupMigrationTests(TestCase):
    def setUp(self) -> None:
        self.location = baker.make(Location, latitude=43.3, longitude=-75.3)
        self.wiki = baker.make(Wiki, location=self.location)
        self.sql = _dedup_sql("dashboard_wiki_aliases")

    def _insert_raw(self, name: str, kind: str = "alternate") -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO dashboard_wiki_aliases (name, kind, source, wiki_id, created, updated) "
                "VALUES (%s, %s, 'user', %s, now(), now())",
                [name, kind, self.wiki.pk],
            )

    def _drop_constraint(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("DROP INDEX db_walias_unique")

    def test_case_insensitive_duplicates_are_collapsed_to_one_row(self) -> None:
        # Wiki creation auto-syncs its own name as an alias, so scope the
        # assertion to this test's own name group rather than the wiki's total.
        self._drop_constraint()
        self._insert_raw("Main Street")
        self._insert_raw("main street")
        with connection.cursor() as cursor:
            cursor.execute(self.sql)
        self.assertEqual(WikiAlias.objects.filter(wiki=self.wiki, name__iexact="Main Street").count(), 1)

    def test_official_kind_is_preferred_as_the_survivor(self) -> None:
        self._drop_constraint()
        self._insert_raw("Main Street", kind="alternate")
        self._insert_raw("main street", kind="official")
        with connection.cursor() as cursor:
            cursor.execute(self.sql)
        remaining = WikiAlias.objects.get(wiki=self.wiki, name__iexact="Main Street")
        self.assertEqual(remaining.kind, "official")
