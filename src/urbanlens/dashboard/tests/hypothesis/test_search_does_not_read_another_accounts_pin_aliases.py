"""Searching your own pins must not read alias rows belonging to a pin you cannot see.

``test_search_does_not_read_another_accounts_labels.py`` already covers ``PinSearchProvider``'s
``labels__name`` path. Its field list (``apply_text``'s call in ``PinSearchProvider.search``) also
feeds ``aliases__name`` - Pin's own reverse foreign key to ``PinAlias`` (``related_name="aliases"``),
distinct from ``ArticleSearchProvider``'s ``pin__aliases__name``
(``test_search_does_not_read_another_accounts_article_aliases.py``): that file drives the semijoin
from ``Article._base_manager``; this one drives it from ``Pin._base_manager`` directly. Same shape,
different driving model, and nothing in ``Label.name``'s migration-0049 GIN trigram index applies to
``PinAlias.name`` - it was never indexed the same way - so both the matching and non-matching variant
are expected to reproduce, matching the Article/Trip/Safety precedent for every relation the index
does not cover.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker
import pytest

from urbanlens.core.tests.explain import relations_read, rows_examined
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PinSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through the viewer's own pin alias.
TERM = "quokka"

#: A word the viewer never searches for, for aliases that must be scanned to be rejected.
OTHER = "wombat"

#: Aliases the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's aliases were read.
TOLERANCE = 20

_REASON = (
    "Same mechanism as P123, over PinSearchProvider's own aliases__name instead of labels__name: the "
    "alias semi-join reads the whole dashboard_pin_aliases table, because it runs against "
    "Pin._base_manager with no access scoping and no index gives the planner a real cardinality "
    "estimate for this column. Fixing it needs the join reordered to drive from the pin, not the alias."
)


class _PinOwnAliasCase(TestCase):
    """A viewer with one pin matching through its own alias, and a stranger's pin with unrelated aliases."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.aliased = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.mine = baker.make(Pin, profile=self.viewer, location=my_location, name="Unrelated", description="")
        baker.make(PinAlias, pin=self.mine, name=f"{TERM} mine")

        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_pin = baker.make(
            Pin, profile=self.stranger, location=their_location, name="Their Spot", description=""
        )
        self.seed_strangers_aliases(FIRST_BATCH)

    def seed_strangers_aliases(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's pin *count* more aliases, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.aliased += 1
            baker.make(PinAlias, pin=self.their_pin, name=f"{word} theirs {self.aliased}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {PinAlias._meta.db_table}, {Pin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's bare-term search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PinSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def alias_reading_statements(self) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
        _, captured = self.search()
        table = PinAlias._meta.db_table
        return [(sql, params) for sql, params in captured if table in sql]

    def rows_read_searching(self) -> int:
        statements = self.alias_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the pin-alias table, so nothing was measured")
        return sum(rows_examined(sql, params) for sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_aliases(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            per_relation = [relations_read(sql, params) for sql, params in self.alias_reading_statements()]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's pin search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} pin aliases of their own - {after - before} more rows for a pin the viewer has "
                f"never earned access to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPinOwnAliasesTests(_PinOwnAliasCase):
    """One account's pin aliases must not be read to answer a different account's pin search."""

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_matching_aliases(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_unrelated_aliases(self) -> None:
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PinOwnAliasCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_pin(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the pin whose alias matches the term, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_aliases_match_the_term(self) -> None:
        self.seed_strangers_aliases(SECOND_BATCH)
        matching = PinAlias.objects.filter(pin=self.their_pin, name__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded aliases do not match the search term"
        )

    def test_the_strangers_unrelated_aliases_do_not_match_the_term(self) -> None:
        self.seed_strangers_aliases(SECOND_BATCH, matching=False)
        matching = PinAlias.objects.filter(pin=self.their_pin, name__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_pin_is_not_visible_to_the_viewer(self) -> None:
        visible = Pin.objects.filter(profile=self.viewer, pk=self.their_pin.pk).count()
        self.assertEqual(visible, 0, "the stranger's pin is visible to the viewer, so reading its aliases is correct")

    def test_the_measured_statement_reads_the_pin_alias_table(self) -> None:
        statements = self.alias_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the pin-alias table")
        touched: set[str] = set()
        for sql, params in statements:
            touched.update(relations_read(sql, params))
        self.assertIn(
            PinAlias._meta.db_table, touched, f"no plan read the pin-alias table; relations read were {sorted(touched)}"
        )
