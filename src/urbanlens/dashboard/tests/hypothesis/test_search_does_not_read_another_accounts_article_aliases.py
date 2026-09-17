"""Searching your own articles must not read alias rows belonging to pins/wikis you cannot see.

P123's mechanism (``test_search_does_not_read_another_accounts_labels.py``) is not specific to
labels: ``ArticleSearchProvider.search`` feeds ``pin__aliases__name`` and ``wiki__aliases__name`` to
``apply_text``, and both cross a to-many relation (``PinAlias``/``WikiAlias`` are reverse foreign
keys, ``related_name="aliases"``), so both go through the same ``_semijoin`` as labels do - an
``Exists(Article._base_manager.filter(...))`` subquery built from the *unfiltered* manager. Article
has no ``labels`` relation at all (``apply_label_clause`` is never called for it), so this file
checks the mechanism through the one to-many relation this provider actually has, not a stand-in for
labels.

Two independent variants, because ``pin`` and ``wiki`` are separate ``OneToOneField``s and Article's
``CheckConstraint`` enforces exactly one is set per row - a pin-hosted article's search never reaches
``wiki__aliases__name`` and vice versa, so the two paths need two separate reproductions.

**Fixed 2026-09-17.** Both paths cross two segments in (``pin``/``wiki`` themselves are single-valued;
``aliases`` is the to-many hop), so this also confirms the bounded, materialised semi-join fixes the
non-matching variant too, not just the matching one - see
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
from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import ArticleSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term - matched only through an alias, never through content/name.
TERM = "quokka"

#: A word the viewer never searches for, for aliases that must be scanned to be rejected.
OTHER = "wombat"

#: Aliases the stranger holds before and after the growth step.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's aliases were read.
TOLERANCE = 20


class _ArticlePinAliasCase(TestCase):
    """A viewer with one pin-hosted article matching through an alias, and a stranger's unrelated pin aliases."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.aliased = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.my_pin = baker.make(Pin, profile=self.viewer, location=my_location, name="My Spot", description="")
        baker.make(PinAlias, pin=self.my_pin, name=f"{TERM} mine")
        self.mine = baker.make(Article, pin=self.my_pin, wiki=None, content="unrelated text")

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
            cursor.execute(f"ANALYZE {PinAlias._meta.db_table}, {Article._meta.db_table}, {Pin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = ArticleSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def alias_reading_statements(
        self,
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the pin-alias table.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search()
        table = PinAlias._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self) -> int:
        captured, statements = self.alias_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the pin-alias table, so nothing was measured")
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
                f"the viewer's article search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} pin aliases of their own - {after - before} more rows for data the viewer cannot see. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPinArticleAliasesTests(_ArticlePinAliasCase):
    """One account's pin aliases must not be read to answer another account's article search."""

    def test_it_does_not_read_a_strangers_matching_pin_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_pin_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class ThePinAliasMeasurementIsRealTests(_ArticlePinAliasCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_article(self) -> None:
        # An Article has no uuid of its own (it is always addressed as a sub-resource of its host,
        # see ArticleSearchProvider.search) - the result's object_uuid is the host pin's.
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.my_pin.uuid)],
            "the viewer's search did not return the article whose pin alias matches the term, so the measured "
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
        from urbanlens.dashboard.models.pin.model import Pin as PinModel

        visible = PinModel.objects.filter(profile=self.viewer, pk=self.their_pin.pk).count()
        self.assertEqual(visible, 0, "the stranger's pin is visible to the viewer, so reading its aliases is correct")

    def test_the_measured_statement_reads_the_pin_alias_table(self) -> None:
        captured, statements = self.alias_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the pin-alias table")
        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        self.assertIn(
            PinAlias._meta.db_table, touched, f"no plan read the pin-alias table; relations read were {sorted(touched)}"
        )


class _ArticleWikiAliasCase(TestCase):
    """A viewer with one wiki-hosted article matching through an alias, and a stranger's unrelated wiki aliases."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile  # community_enabled defaults to True
        self.stranger = baker.make(User).profile
        self.aliased = 0

        my_location = baker.make(Location, latitude=45.100001, longitude=-71.100001)
        baker.make(Pin, profile=self.viewer, location=my_location, name="My Spot", description="")
        self.my_wiki = baker.make(Wiki, location=my_location, name="My Wiki", description="")
        baker.make(WikiAlias, wiki=self.my_wiki, name=f"{TERM} mine")
        self.mine = baker.make(Article, pin=None, wiki=self.my_wiki, content="unrelated text")

        their_location = baker.make(Location, latitude=45.200002, longitude=-71.200002)
        self.their_wiki = baker.make(Wiki, location=their_location, name="Their Wiki", description="")
        self.seed_strangers_aliases(FIRST_BATCH)

    def seed_strangers_aliases(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger's wiki *count* more aliases, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.aliased += 1
            baker.make(WikiAlias, wiki=self.their_wiki, name=f"{word} theirs {self.aliased}")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {WikiAlias._meta.db_table}, {Article._meta.db_table}, {Wiki._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = ArticleSearchProvider().search(self.viewer, parse_query(TERM), 20)
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
                f"the viewer's article search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} wiki aliases of their own - {after - before} more rows for data the viewer cannot see. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsWikiArticleAliasesTests(_ArticleWikiAliasCase):
    """One account's wiki aliases must not be read to answer another account's article search."""

    def test_it_does_not_read_a_strangers_matching_wiki_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    def test_it_does_not_read_a_strangers_unrelated_wiki_aliases(self) -> None:
        """Fixed 2026-09-17 by the bounded semi-join - see the module docstring."""
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheWikiAliasMeasurementIsRealTests(_ArticleWikiAliasCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_article(self) -> None:
        # An Article has no uuid of its own (it is always addressed as a sub-resource of its host,
        # see ArticleSearchProvider.search) - the result's object_uuid is the host wiki's.
        results, _ = self.search()
        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.my_wiki.uuid)],
            "the viewer's search did not return the article whose wiki alias matches the term, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_aliases_match_the_term(self) -> None:
        self.seed_strangers_aliases(SECOND_BATCH)
        matching = WikiAlias.objects.filter(wiki=self.their_wiki, name__icontains=TERM).count()
        self.assertEqual(
            matching, FIRST_BATCH + SECOND_BATCH, "the stranger's seeded aliases do not match the search term"
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
