"""Searching wikis must not read alias rows belonging to a wiki you cannot see.

``test_search_does_not_read_another_accounts_wiki_labels.py`` already covers ``WikiSearchProvider``'s
`label:`/`tag:` operator. Its bare-term path (``apply_text`` in ``WikiSearchProvider.search``) also
feeds ``aliases__name`` - Wiki's own reverse foreign key to ``WikiAlias`` (``related_name="aliases"``),
distinct from ``ArticleSearchProvider``'s ``wiki__aliases__name``
(``test_search_does_not_read_another_accounts_article_aliases.py``, driven from ``Article``) and from
``PinSearchProvider``'s ``location__wiki__aliases__name``
(``test_search_does_not_read_another_accounts_pin_wiki_aliases.py``, driven from ``Pin``): here the
semijoin is built from ``Wiki._base_manager`` directly. No index covers ``WikiAlias.name`` for this
``icontains`` path, so both the matching and non-matching variant reproduced.

**Fixed 2026-09-17.** ``aliases`` crosses at *path*'s own first segment, so ``_semijoin`` now bounds
its filter by the outer queryset's own candidate primary keys, materialised as a concrete list first
rather than left as a nested subquery. See
``test_search_does_not_read_another_accounts_labels.py``'s module docstring for the measured
mechanism and ``docs/archive/PROBLEMS-ARCHIVE.md`` (formerly P123) for the full writeup.
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
from urbanlens.dashboard.services.global_search.providers import WikiSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through the viewer's own wiki's alias.
TERM = "quokka"

#: A word the viewer never searches for, for aliases that must be scanned to be rejected.
OTHER = "wombat"

#: Aliases the stranger's wiki holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's wiki aliases were read.
TOLERANCE = 20


class _WikiOwnAliasCase(TestCase):
    """A viewer with one visible wiki matching through its own alias, and a stranger's wiki's unrelated aliases."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile  # community_enabled defaults to True
        self.stranger = baker.make(User).profile
        self.aliased = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        # visible_wiki_locations_cached requires either a pin at the location or an accessible
        # domain match (wiki_access.py) - a bare Wiki with no pin is invisible to its own creator.
        baker.make(Pin, profile=self.viewer, location=my_location, name="Unrelated", description="")
        self.mine = baker.make(Wiki, location=my_location, name="Unrelated", description="")
        baker.make(WikiAlias, wiki=self.mine, name=f"{TERM} mine")

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
            cursor.execute(f"ANALYZE {WikiAlias._meta.db_table}, {Wiki._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's bare-term wiki search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = WikiSearchProvider().search(self.viewer, parse_query(TERM), 20)
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
                f"the viewer's wiki search read {before} rows, then {after} after a stranger's wiki gained "
                f"{count} {kind} aliases of its own - {after - before} more rows for a wiki the viewer "
                f"has never earned access to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsWikiOwnAliasesTests(_WikiOwnAliasCase):
    """One wiki's aliases must not be read to answer a search over a different wiki."""

    def test_it_does_not_read_a_strangers_matching_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_WikiOwnAliasCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_wiki(self) -> None:
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the wiki whose alias matches the term, so the measured "
            "statement is not the one the defect is about",
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

    def test_the_strangers_wiki_is_not_visible_to_the_viewer(self) -> None:
        from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids

        self.assertNotIn(
            self.their_wiki.location_id,
            visible_wiki_location_ids(self.viewer),
            "the stranger's wiki is visible to the viewer, so reading its aliases is correct",
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
