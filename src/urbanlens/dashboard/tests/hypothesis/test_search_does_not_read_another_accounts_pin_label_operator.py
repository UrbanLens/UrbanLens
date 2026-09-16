"""Searching pins via `label:` must not read label rows belonging to a pin you cannot see.

``test_search_does_not_read_another_accounts_labels.py`` already covers ``PinSearchProvider``'s
bare-term path (``"labels__name"`` in ``apply_text``'s field list, ``icontains``). That file's own
docstring left one thing unchecked: "Whether Pin's and Photo's own `label:` operator... has the same
gap was not checked." This file is that check, for Pin.

``PinSearchProvider.search`` also calls ``apply_label_clause`` for the `label:`/`tag:` operator,
which filters through the same ``_semijoin(Pin, "labels", ...)`` shape as the bare-term path -
``Exists(Pin._base_manager.filter(condition, pk=OuterRef("pk")))``, unscoped to pins the viewer
owns. The one difference: `label:` matches with ``iexact``, not ``icontains``. P123's fix (a GIN
trigram index on ``Label.name``, migration 0049) targets ``icontains``'s compiled form specifically,
so it was expected to say nothing about an ``iexact`` plan - the same result already found for
Wiki's identical call to ``apply_label_clause``
(``test_search_does_not_read_another_accounts_wiki_labels.py``), both of whose variants stay open.

**Measured here rather than assumed, Pin's matching variant does not match that expectation.**
Confirmed twice in isolation (a single-test run, and this file run alone, both against a fresh test
database): the matching-term case does not read more of the label table as a stranger's pin gains
400 unrelated labels, while the non-matching case does, exactly like Wiki and
``test_search_does_not_read_another_accounts_photo_label_operator.py``'s Photo equivalent (both
variants open). Not decomposed further - the divergence is plausibly the same
statistics-driven-join-reordering effect the module docstring of
``test_search_does_not_read_another_accounts_labels.py`` describes for ``icontains`` (a precise
cardinality estimate changing the enclosing plan's join order, not the label scan itself), now
triggered by ``iexact``'s own comparison rather than the trigram index - but that would have to
explain why the same mechanism does not equally rescue Photo's or Wiki's matching variant, and that
was not chased down; recorded as a measured fact, not a diagnosed one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker
import pytest

from urbanlens.core.tests.explain import relations_read, rows_examined
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PinSearchProvider

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The exact name of the viewer's own label - `label:` matches on the whole string (iexact), so
#: there is nothing to search for "containing" this the way the bare-term icontains variant needs.
MINE = "quokka-mine"

#: A name that matches nothing anywhere, for the true-negative variant.
NOWHERE = "wombat-nowhere"

#: Labels the stranger's pin holds before and after the growth step. Never equal to MINE or NOWHERE.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's labels were read.
TOLERANCE = 20

_REASON = (
    "Same semijoin as P123, over apply_label_clause's iexact match instead of icontains: the whole "
    "label table is read regardless of a stranger's growing, unrelated labels, because the semijoin "
    "runs against Pin._base_manager with no access scoping at all. Fixing it needs the join reordered "
    "to drive from the pin, not the label, same as the bare-term path."
)


class _PinLabelOperatorCase(TestCase):
    """A viewer with one pin (one exactly-named label), and a stranger's pin whose unrelated labels
    share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.placed = 0
        self.labelled = 0

        self.mine = self._pin(self.viewer)
        self.mine.labels.add(baker.make(Label, profile=self.viewer, kind=KIND_TAG, name=MINE))
        self.their_pin = self._pin(self.stranger)
        self.seed_strangers_labels(FIRST_BATCH)

    def _pin(self, profile: Profile) -> Pin:
        """A pin at its own coordinates, since locations are unique on (latitude, longitude)."""
        self.placed += 1
        location = baker.make(
            Location,
            latitude=f"44.{self.placed:06d}",
            longitude=f"-70.{self.placed:06d}",
        )
        return baker.make(Pin, profile=profile, location=location, name=f"Spot {self.placed}", description="")

    def seed_strangers_labels(self, count: int) -> None:
        """Give the stranger's pin *count* more labels, none of them equal to MINE or NOWHERE, then re-analyse."""
        labels = []
        for _ in range(count):
            self.labelled += 1
            labels.append(
                baker.make(Label, profile=self.stranger, kind=KIND_TAG, name=f"stranger-label-{self.labelled}")
            )
        self.their_pin.labels.add(*labels)
        with connection.cursor() as cursor:
            cursor.execute(
                f"ANALYZE {Label._meta.db_table}, {Pin._meta.db_table}, {Pin.labels.through._meta.db_table}",  # noqa: S608 - table names from the ORM, not from input
            )

    def search(self, term: str) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run `label:"term"` as the viewer, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PinSearchProvider().search(self.viewer, parse_query(f'label:"{term}"'), 20)
        return list(results), captured

    def label_reading_statements(self, term: str) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
        """The statements of a `label:"term"` search that read the label table at all."""
        _, captured = self.search(term)
        table = Label._meta.db_table
        return [(sql, params) for sql, params in captured if table in sql]

    def rows_read_searching(self, term: str) -> int:
        """Rows a `label:"term"` search reads, across every statement touching the label table."""
        statements = self.label_reading_statements(term)
        self.assertTrue(statements, "no statement of the search mentioned the label table, so nothing was measured")
        return sum(rows_examined(sql, params) for sql, params in statements)

    def assertDoesNotGrow(self, term: str) -> None:
        """Assert a `label:"term"` search reads no more rows after the stranger's pin gains more unrelated labels."""
        before = self.rows_read_searching(term)

        self.seed_strangers_labels(SECOND_BATCH)

        after = self.rows_read_searching(term)
        if after > before + TOLERANCE:
            per_relation = [relations_read(sql, params) for sql, params in self.label_reading_statements(term)]
            self.fail(
                f"the viewer's label:\"{term}\" search read {before} rows, then {after} after a stranger's pin "
                f"gained {SECOND_BATCH} unrelated labels - {after - before} more rows for a pin the viewer has "
                f"never earned access to. Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsPinLabelOperatorTests(_PinLabelOperatorCase):
    """One account's labels must not be read to answer a `label:` search for a different account's pin."""

    def test_it_does_not_read_a_strangers_labels_when_the_term_matches_elsewhere(self) -> None:
        """A `label:` search that DOES match the viewer's own pin must not also scan the stranger's.

        Not xfailed: measured, not assumed, to already hold - see the module docstring. Unlike Photo's
        and Wiki's identical call to `apply_label_clause`, Pin's matching variant does not grow here.
        """
        self.assertDoesNotGrow(MINE)

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_labels_when_the_term_matches_nothing(self) -> None:
        """A `label:` search that matches nothing anywhere is the true negative: nothing to short-circuit on."""
        self.assertDoesNotGrow(NOWHERE)


class TheMeasurementIsRealTests(_PinLabelOperatorCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_pin(self) -> None:
        """Proves `label:"MINE"` matched through the label rather than returning early or matching nothing."""
        results, _ = self.search(MINE)

        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's label search did not return the pin whose label matches, so the measured "
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
        ).filter(
            name__iexact=NOWHERE,
        )

        self.assertEqual(colliding.count(), 0, "a stranger's seeded label collided with MINE or NOWHERE by name")

    def test_the_stranger_is_not_visible_to_the_viewer(self) -> None:
        """The premise of the whole file: these labels are none of the viewer's business."""
        visible = Label.objects.visible_to(self.viewer).filter(profile=self.stranger).count()

        self.assertEqual(visible, 0, "the stranger's labels are visible to the viewer, so reading them is correct")

    def test_the_measured_statement_reads_the_label_table(self) -> None:
        """Proves the plan being summed actually touches labels, rather than being some other statement."""
        statements = self.label_reading_statements(MINE)
        self.assertTrue(statements, "the search issued no statement mentioning the label table")

        touched: set[str] = set()
        for sql, params in statements:
            touched.update(relations_read(sql, params))

        self.assertIn(
            Label._meta.db_table,
            touched,
            f"no plan read the label table; relations read were {sorted(touched)}",
        )
