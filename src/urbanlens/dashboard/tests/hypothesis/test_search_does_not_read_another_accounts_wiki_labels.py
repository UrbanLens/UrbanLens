"""Searching wikis via `label:` must not read label rows belonging to a wiki you cannot see.

Unlike Pin and Photo, ``WikiSearchProvider.search`` does not put ``"labels__name"`` in the field
list it hands to ``apply_text`` (``providers.py``, ``WikiSearchProvider.search``) - so a bare
search term never reaches the label table for a wiki at all. That is a real, separate gap (a user
cannot find their own wiki by searching the text of a label they put on it) and is not this file's
subject; it is noted here because it means this file cannot mirror the Pin/Photo reproductions'
bare-term query and had to find the path Wiki actually shares with them.

That shared path is ``apply_label_clause`` (`label:`/`tag:` operator, ``providers.py``), called
identically by ``PinSearchProvider``, ``PhotoSearchProvider`` and ``WikiSearchProvider``. It filters
through the same ``_semijoin(Wiki, "labels", ...)`` as P123
(``test_search_does_not_read_another_accounts_labels.py``) - an
``Exists(Wiki._base_manager.filter(...))`` subquery built from the *unfiltered* manager, unscoped to
wikis the viewer has earned access to. A stranger's wiki, at a place the viewer has never pinned per
``dashboard/models/wiki/CLAUDE.md``, never needs to be visible to the viewer for its labels to sit in
the same ``dashboard_labels`` table the semijoin's inner query scans.

The one structural difference from P123/Photo worth flagging: `label:` matches with ``iexact``, not
``icontains``. P123's fix (a GIN trigram index on ``Label.name``, migration 0049) targets
``icontains``'s compiled form specifically; measured here (not assumed), it says nothing about an
``iexact`` plan at all - so unlike Photo, where the index alone helped the matching variant, *both*
variants of this path needed the bounded-semi-join rewrite instead. Rows examined grew by the full
amount of unrelated stranger labels added, whether or not the search term matched anything of the
viewer's, before the fix.

**Fixed 2026-09-17.** ``_semijoin`` now bounds ``labels``' filter by the outer queryset's own
candidate primary keys, materialised as a concrete list first. See
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
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import WikiSearchProvider
from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The exact name of the viewer's own label - `label:` matches on the whole string (iexact), so
#: there is nothing to search for "containing" this the way P123's icontains variant needs.
MINE = "quokka-mine"

#: A name that matches nothing anywhere, for the true-negative variant.
NOWHERE = "wombat-nowhere"

#: Labels the stranger's wiki holds before and after the growth step. None of these ever equal
#: MINE or NOWHERE - iexact's uniqueness constraint on (lower(name), profile, kind) means a
#: "matching-at-scale" variant would need many stranger *profiles*, not many stranger *labels*, so
#: what both variants below actually vary is unrelated table size, not match count.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's labels were read.
TOLERANCE = 20


class _WikiLabelOperatorCase(TestCase):
    """A viewer with access to one wiki (one exactly-named label), and a stranger's inaccessible
    wiki whose unrelated labels share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.labelled = 0

        # The viewer earns access the only way `dashboard/models/wiki/CLAUDE.md` allows: their own
        # pin at the wiki's exact location.
        self.my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        baker.make(Pin, profile=self.viewer, location=self.my_location, name="My Spot", description="")
        self.mine = baker.make(Wiki, location=self.my_location, name="My Wiki", description="")
        self.mine.labels.add(baker.make(Label, profile=self.viewer, kind=KIND_TAG, name=MINE))

        # The stranger's wiki sits at a place the viewer has never pinned - inaccessible by the
        # domain rule, so it must never appear as a search candidate for the viewer.
        self.their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_wiki = baker.make(Wiki, location=self.their_location, name="Their Wiki", description="")
        self.seed_strangers_labels(FIRST_BATCH)

    def seed_strangers_labels(self, count: int) -> None:
        """Give the stranger's wiki *count* more labels, none of them equal to MINE or NOWHERE, then re-analyse."""
        labels = []
        for _ in range(count):
            self.labelled += 1
            labels.append(
                baker.make(Label, profile=self.stranger, kind=KIND_TAG, name=f"stranger-label-{self.labelled}")
            )
        self.their_wiki.labels.add(*labels)
        with connection.cursor() as cursor:
            cursor.execute(
                f"ANALYZE {Label._meta.db_table}, {Wiki._meta.db_table}, {Wiki.labels.through._meta.db_table}",  # noqa: S608 - table names from the ORM, not from input
            )

    def search(self, term: str) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run `label:"term"` as the viewer, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = WikiSearchProvider().search(self.viewer, parse_query(f'label:"{term}"'), 20)
        return list(results), captured

    def label_reading_statements(
        self, term: str
    ) -> tuple[
        list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
        list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]],
    ]:
        """The full capture, and the statements in it that read the label table at all.

        Returning both, rather than just the filtered statements, lets a caller pass the
        filtered statements' original positions to ``session_preamble`` - a statement's plan
        depends on session GUCs active when it ran (see ``_semijoin``'s ``enable_seqscan``
        bracket), not just its SQL text, and that bracket's position is only meaningful against
        the one capture it came from.
        """
        _, captured = self.search(term)
        table = Label._meta.db_table
        return captured, [(i, sql, params) for i, (sql, params) in enumerate(captured) if table in sql]

    def rows_read_searching(self, term: str) -> int:
        """Rows a `label:"term"` search reads, across every statement touching the label table."""
        captured, statements = self.label_reading_statements(term)
        self.assertTrue(statements, "no statement of the search mentioned the label table, so nothing was measured")
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def assertDoesNotGrow(self, term: str) -> None:
        """Assert a `label:"term"` search reads no more rows after the stranger's wiki gains more unrelated labels."""
        before = self.rows_read_searching(term)

        self.seed_strangers_labels(SECOND_BATCH)

        after = self.rows_read_searching(term)
        if after > before + TOLERANCE:
            captured, statements = self.label_reading_statements(term)
            per_relation = [
                relations_read(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements
            ]
            self.fail(
                f"the viewer's label:\"{term}\" search read {before} rows, then {after} after a stranger's wiki "
                f"gained {SECOND_BATCH} unrelated labels - {after - before} more rows for a wiki the viewer has "
                f"never earned access to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsWikiLabelsTests(_WikiLabelOperatorCase):
    """One wiki's labels must not be read to answer a `label:` search for a different, accessible wiki."""

    def test_it_does_not_read_a_strangers_labels_when_the_term_matches_elsewhere(self) -> None:
        """A `label:` search that DOES match the viewer's own wiki must not also scan the stranger's.

        Unlike Photo's matching variant, the trigram index never touched this one: it targets
        icontains, and `label:` matches with iexact, a different compiled query. Fixed 2026-09-17 by
        the bounded semi-join instead - see the module docstring.
        """
        self.assertDoesNotGrow(MINE)

    def test_it_does_not_read_a_strangers_labels_when_the_term_matches_nothing(self) -> None:
        """A `label:` search that matches nothing anywhere is the true negative: nothing to short-circuit on.

        Fixed 2026-09-17 by the bounded semi-join - see the module docstring.
        """
        self.assertDoesNotGrow(NOWHERE)


class TheMeasurementIsRealTests(_WikiLabelOperatorCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_wiki(self) -> None:
        """Proves `label:"MINE"` matched through the label rather than returning early or matching nothing."""
        results, _ = self.search(MINE)

        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's label search did not return the wiki whose label matches, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_term_that_matches_nothing_really_matches_nothing(self) -> None:
        """The premise of the non-matching variant: NOWHERE must not equal any seeded label, anywhere."""
        self.seed_strangers_labels(SECOND_BATCH)

        self.assertEqual(
            Label.objects.filter(name__iexact=NOWHERE).count(),
            0,
            "NOWHERE matched a real label, so the non-matching variant is measuring something else",
        )

    def test_the_strangers_labels_never_collide_with_mine_or_nowhere(self) -> None:
        """The premise of the growth step: the stranger's added rows are unrelated filler, not incidental matches."""
        self.seed_strangers_labels(SECOND_BATCH)

        colliding = Label.objects.filter(profile=self.stranger).filter(name__iexact=MINE) | Label.objects.filter(
            profile=self.stranger
        ).filter(name__iexact=NOWHERE)

        self.assertEqual(colliding.count(), 0, "a stranger's seeded label collided with MINE or NOWHERE by name")

    def test_the_stranger_wiki_is_not_visible_to_the_viewer(self) -> None:
        """The premise of the whole file: this wiki is none of the viewer's business."""
        self.assertNotIn(
            self.their_location.pk,
            visible_wiki_location_ids(self.viewer),
            "the stranger's wiki is visible to the viewer, so reading its labels is correct and this file "
            "is testing nothing",
        )

    def test_the_measured_statement_reads_the_label_table(self) -> None:
        """Proves the plan being summed actually touches labels, rather than being some other statement."""
        captured, statements = self.label_reading_statements(MINE)
        self.assertTrue(statements, "the search issued no statement mentioning the label table")

        touched: set[str] = set()
        for i, sql, params in statements:
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))

        self.assertIn(
            Label._meta.db_table,
            touched,
            f"no plan read the label table; relations read were {sorted(touched)}",
        )
