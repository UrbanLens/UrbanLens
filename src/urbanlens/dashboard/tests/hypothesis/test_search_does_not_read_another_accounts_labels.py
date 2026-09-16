"""Searching your own pins must not read label rows belonging to accounts you cannot see.

The pins provider matches ``labels__name`` through ``_semijoin``, which builds
``Exists(Pin._base_manager.filter(condition, pk=OuterRef("pk")))``. Measured on the capacity population (X20),
the resulting plan walked all 62,354 labels on the site to answer one account's search.

The mechanism, read off ``EXPLAIN (ANALYZE, FORMAT JSON)`` rather than inferred: the labels semi-join runs a
**sequential scan of the whole label table as the outer side of a nested loop**, probing the pin by primary key
afterwards. ``name__icontains`` is a leading-wildcard ILIKE, which no btree index serves and whose selectivity
Postgres estimated at one row while it actually yielded hundreds, so starting from the label table looked cheap.

Adding the outer query's ``profile`` predicate to the subquery was measured and is a **no-op**: it is applied
(it appears on ``u0`` in the plan) but the pin is reached by primary key on the inner side, so it cannot
reorder the join. Row counts moved by 2 of 1,191. Do not re-attempt that without a new measurement.

**A functional GIN trigram index on** ``upper(name::text)`` **(migration 0049) fixes the matching variant, not
the non-matching one.** Confirmed by direct causation test: dropping the index in a live test database flips
``test_it_does_not_read_a_strangers_matching_labels`` from passing back to its old failure, and recreating it
flips it back - the index, not something else, is doing this. The labels-table scan node itself is still a Seq
Scan with the index dropped or present (``EXPLAIN`` shows no ``Index Scan`` either way at this table's size,
which is far below where Postgres would consider a bitmap/index scan competitive with a sequential one) - the
benefit is from better statistics, not a different scan strategy. A GIN/GiST expression index makes ``ANALYZE``
collect real cardinality statistics for that expression, so Postgres's estimate for ``UPPER(name) LIKE
UPPER(%s)`` stops being a fixed default and starts tracking the term's real match count; that appears to change
how the *enclosing* query plans (join order / short-circuiting), not the labels node in isolation. Not fully
decomposed further - the win was reproducible and cheap enough that finishing that decomposition wasn't
worth it.

The non-matching variant is unaffected: every added label is a true negative, so nothing short-circuits the
scan early and the full table (now including all the added noise) is still read to conclude no match exists.
Fixing that needs the join reordered so the *pin*, not the label, drives the search - see the class docstring
and ``docs/PROBLEMS.md`` P123 for the untaken direction and why it's out of scope here (it needs generic
reverse-relation derivation across all ten search providers, some of whose paths put the many-crossing relation
after the first hop).

The axis here is rows *read*, not rows returned: the subquery returns almost nothing whatever it scans, so a
query counter, a ``rowcount`` wrapper and a response-size budget all read this as flat. That is why
``EndpointScalingMixin`` cannot express this defect.
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

#: The viewer's search term.
TERM = "quokka"

#: A word the viewer never searches for, for labels that must be scanned to be rejected.
OTHER = "wombat"

#: Labels the stranger holds before and after the growth step. The second is large enough that a plan reading
#: them is unmistakable against the viewer's single matching label.
FIRST_BATCH = 2
SECOND_BATCH = 400

#: Rows the measurement may drift by without meaning the stranger's labels were read. Small: the viewer's own
#: side of the search does not change between the two measurements.
TOLERANCE = 20

_REASON = (
    "P123: the labels semi-join still reads the whole label table for a non-matching term, because a true "
    "negative can't short-circuit the scan. The trigram index (migration 0049) fixed the matching variant "
    "but not this one - fixing it needs the join reordered to drive from the pin, not the label."
)


class _PinSearchCase(TestCase):
    """A viewer with one matching pin, and a stranger whose labels share the table."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.placed = 0
        self.labelled = 0

        self.mine = self._pin(self.viewer)
        self.mine.labels.add(baker.make(Label, profile=self.viewer, kind=KIND_TAG, name=f"{TERM} mine"))
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

    def seed_strangers_labels(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger *count* more labels, then re-analyse.

        Attached to the stranger's pin rather than left loose: a label reachable from no pin can be dropped by
        the join before the plan reads it, which would make the growth step seed nothing the search could see.

        Args:
            count: How many labels to add.
            matching: Whether their names contain the viewer's search term. Matching labels measure rows the
                plan reads *and* discards at the pin join; non-matching ones measure rows it reads only to
                reject, which is the larger share of the cost on a real population and the part an index on
                the name could remove.
        """
        word = TERM if matching else OTHER
        labels = []
        for _ in range(count):
            self.labelled += 1
            labels.append(
                baker.make(Label, profile=self.stranger, kind=KIND_TAG, name=f"{word} theirs {self.labelled}"),
            )
        self.their_pin.labels.add(*labels)
        with connection.cursor() as cursor:
            cursor.execute(
                f"ANALYZE {Label._meta.db_table}, {Pin._meta.db_table}, {Pin.labels.through._meta.db_table}",  # noqa: S608 - table names from the ORM, not from input
            )

    def search(self) -> tuple[list, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run the viewer's search, capturing every statement it issued."""
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            results = PinSearchProvider().search(self.viewer, parse_query(TERM), 20)
        return list(results), captured

    def label_reading_statements(self) -> list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]:
        """The statements of the viewer's search that read the label table at all."""
        _, captured = self.search()
        table = Label._meta.db_table
        return [(sql, params) for sql, params in captured if table in sql]

    def rows_read_searching(self) -> int:
        """Rows the viewer's search reads, across every statement touching the label table."""
        statements = self.label_reading_statements()
        self.assertTrue(statements, "no statement of the search mentioned the label table, so nothing was measured")
        return sum(rows_examined(sql, params) for sql, params in statements)

    def assertDoesNotGrow(self, count: int, *, matching: bool) -> None:
        """Assert the viewer's search reads no more rows after the stranger gains *count* labels."""
        before = self.rows_read_searching()

        self.seed_strangers_labels(count, matching=matching)

        after = self.rows_read_searching()
        if after > before + TOLERANCE:
            per_relation = [relations_read(sql, params) for sql, params in self.label_reading_statements()]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"the viewer's search read {before} rows, then {after} after a stranger added {count} "
                f"{kind} labels of their own - {after - before} more rows for data the viewer cannot see. "
                f"Rows read per relation: {per_relation}",
            )


class SearchDoesNotReadAnotherAccountsLabelsTests(_PinSearchCase):
    """P123: one account's labels must not be read to answer another account's search."""

    def test_it_does_not_read_a_strangers_matching_labels(self) -> None:
        """Rows the plan reads and then discards at the pin join, because the pin is not the viewer's.

        Fixed by the trigram index in migration 0049 - see the module docstring for the measured mechanism.
        """
        self.assertDoesNotGrow(SECOND_BATCH, matching=True)

    @pytest.mark.xfail(strict=True, reason=_REASON)
    def test_it_does_not_read_a_strangers_unrelated_labels(self) -> None:
        """Rows the plan reads only to reject on the name.

        Separated from the matching case because the two have different fixes: an index on the label name
        would remove these and leave the matching ones, so a candidate fix that passes one test and not the
        other has done half the job.
        """
        self.assertDoesNotGrow(SECOND_BATCH, matching=False)


class TheMeasurementIsRealTests(_PinSearchCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_pin(self) -> None:
        """Proves the search matched through a label rather than returning early or matching nothing."""
        results, _ = self.search()

        self.assertEqual(
            [result.object_uuid for result in results],
            [str(self.mine.uuid)],
            "the viewer's search did not return the pin whose label matches the term, so the measured "
            "statement is not the one the defect is about",
        )

    def test_the_strangers_matching_labels_match_the_term(self) -> None:
        """Proves the seeded rows are rows a scan would have to look at, not inert filler."""
        self.seed_strangers_labels(SECOND_BATCH)

        matching = Label.objects.filter(profile=self.stranger, name__icontains=TERM).count()

        self.assertEqual(
            matching,
            FIRST_BATCH + SECOND_BATCH,
            "the stranger's seeded labels do not match the search term, so the matching-labels test is "
            "measuring the non-matching case twice",
        )

    def test_the_strangers_unrelated_labels_do_not_match_the_term(self) -> None:
        """The premise of the non-matching variant: these rows can only be read to be rejected."""
        self.seed_strangers_labels(SECOND_BATCH, matching=False)

        matching = Label.objects.filter(profile=self.stranger, name__icontains=TERM).count()

        self.assertEqual(
            matching,
            FIRST_BATCH,
            "the non-matching seed matched the search term after all, so the two variants measure the same "
            "thing and neither can tell a partial fix from a whole one",
        )

    def test_the_stranger_is_not_visible_to_the_viewer(self) -> None:
        """The premise of the whole file: these labels are none of the viewer's business."""
        visible = Label.objects.visible_to(self.viewer).filter(profile=self.stranger).count()

        self.assertEqual(visible, 0, "the stranger's labels are visible to the viewer, so reading them is correct")

    def test_the_measured_statement_reads_the_label_table(self) -> None:
        """Proves the plan being summed actually touches labels, rather than being some other statement."""
        statements = self.label_reading_statements()
        self.assertTrue(statements, "the search issued no statement mentioning the label table")

        touched: set[str] = set()
        for sql, params in statements:
            touched.update(relations_read(sql, params))

        self.assertIn(
            Label._meta.db_table,
            touched,
            f"no plan read the label table; relations read were {sorted(touched)}",
        )
