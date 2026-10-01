"""Searching your own pins must not read wiki-alias rows belonging to a wiki you cannot see.

``PinSearchProvider.search`` feeds ``location__wiki__aliases__name`` to ``apply_text`` - a path to
``WikiAlias`` distinct from both ``ArticleSearchProvider``'s ``wiki__aliases__name``
(``test_search_does_not_read_another_accounts_article_aliases.py``, driven from ``Article``) and
``WikiSearchProvider``'s own ``aliases__name`` (driven from ``Wiki`` directly): here the crossing
relation sits at the *third* path segment (``location`` then ``wiki`` are both single-valued hops),
and the semijoin is built from ``Pin._base_manager``. ``_crosses_many`` only looks at whether *any*
segment crosses a to-many relation, so this still triggers the same unscoped
``Exists(Pin._base_manager.filter(location__wiki__aliases__name__icontains=..., pk=OuterRef("pk")))``
- one more account's pins can be slowed down by a completely unrelated wiki's alias count. No index
covers ``WikiAlias.name`` for this ``icontains`` path (migration 0049 only targets ``Label.name``).

**Fixed 2026-09-17.** The crossing relation sitting three segments in does not change the fix: bounding
and materialising the semi-join's candidate primary keys, and forcing the index plan for that internal
lookup, fixes the non-matching variant here too - see
``test_search_does_not_read_another_accounts_labels.py``'s module docstring for the measured mechanism
and ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for the full writeup.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PinSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through the viewer's own pin's wiki's alias.
TERM = "quokka"

#: A word the viewer never searches for, for aliases that must be scanned to be rejected.
OTHER = "wombat"

#: Aliases the stranger's wiki holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's wiki aliases were read.
TOLERANCE = 20


class _PinWikiAliasCase(TestCase):
    """A viewer whose pin's own wiki matches through an alias, and a stranger's unrelated wiki's aliases."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.aliased = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.mine = baker.make(Pin, profile=self.viewer, location=my_location, name="Unrelated", description="")
        self.my_wiki = baker.make(Wiki, location=my_location, name="My Wiki", description="")
        baker.make(WikiAlias, wiki=self.my_wiki, name=f"{TERM} mine")

        # The stranger's wiki is reached through a location with no pin at all - the viewer's pin
        # search must never need one to exist for the unscoped semijoin to still read it.
        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_wiki = baker.make(Wiki, location=their_location, name="Their Wiki", description="")
        self.seed_strangers_aliases(FIRST_BATCH)

    def seed_strangers_aliases(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's wiki *count* more aliases, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.aliased += 1
            baker.make(WikiAlias, wiki=self.their_wiki, name=f"{word} theirs {self.aliased}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {WikiAlias._meta.db_table}, {Wiki._meta.db_table}, {Pin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's bare-term pin search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PinSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def alias_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the wiki-alias table.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search()
        table = WikiAlias._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.alias_reading_statements()
        self.assertTrue(
            statements, "no statement of the search mentioned the wiki-alias table, so nothing was measured"
        )
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.seed_strangers_aliases(count, matching=matching)
        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            captured, statements = self.alias_reading_statements()
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's pin search read {before} rows, then {after} after an unrelated wiki gained "
                f"{count} {kind} aliases of its own - {after - before} more rows for a wiki the viewer's "
                f"pin has nothing to do with. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPinWikiAliasesTests(_PinWikiAliasCase):
    """An unrelated wiki's aliases must not be read to answer a pin search over a different wiki."""

    def test_it_does_not_read_a_strangers_matching_wiki_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_wiki_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PinWikiAliasCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_pin(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the pin whose wiki alias matches the term, so the "
            "measured statement is not the one the defect is about",
        )

    def test_the_strangers_matching_aliases_match_the_term(self) -> None:
        self.seed_strangers_aliases(SECOND_BATCH)
        matching = WikiAlias.objects.filter(wiki=self.their_wiki, name__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded wiki aliases do not match the search term"
        )

    def test_the_strangers_unrelated_aliases_do_not_match_the_term(self) -> None:
        self.seed_strangers_aliases(SECOND_BATCH, matching=False)
        matching = WikiAlias.objects.filter(wiki=self.their_wiki, name__icontains=TERM).count()
        self.assertEqual(matching, FIRST_BATCH, "the non-matching seed matched the search term after all")

    def test_the_strangers_wiki_has_no_relation_to_the_viewers_pin(self) -> None:
        self.assertNotEqual(
            self.their_wiki.location_id,
            self.mine.location_id,
            "the stranger's wiki shares the viewer's pin's location, so it is not an unrelated wiki",
        )

    def test_the_measured_statement_reads_the_wiki_alias_table(self) -> None:
        captured, statements = self.alias_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the wiki-alias table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            WikiAlias._meta.db_table,
            touched,
            f"no plan read the wiki-alias table; relations read were {sorted(touched)}",
        )
