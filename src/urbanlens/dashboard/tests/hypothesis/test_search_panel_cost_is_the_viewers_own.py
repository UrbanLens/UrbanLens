"""What one global search costs, measured across the whole provider fan-out rather than one provider.

The ``test_search_does_not_read_another_accounts_*`` family each drive a single provider directly,
so a provider added later is covered by nothing until someone writes its file, and neither the
engine's fan-out nor its plain-text fallback is ever inside a measurement. This drives
:class:`GlobalSearchEngine` the way the panel does and asserts the two properties that decide
whether search scales with users:

* **Rows read are the viewer's own.** A stranger adding rows must not make the viewer's search read
  more of the database. This is the property P123 and P100 are both about, applied to every
  provider at once.
* **Statements are the viewer's own.** The count must not grow with anybody's row count - neither a
  stranger's (which would be a leak) nor the viewer's own (which would be an N+1).

The absolute ceilings below are ratchets, not targets: when a change lowers one, lower it here too,
so the next regression has something to fail against.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read, rows_examined, session_preamble
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.services.global_search.engine import GlobalSearchEngine

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: The viewer's search term, matched through several of the viewer's own relations.
TERM = "quokka"

#: A word the viewer never searches for, for rows that must be scanned to be rejected.
OTHER = "wombat"

#: How much the stranger grows by between the two measurements.
GROWTH = 300

#: Rows the measurement may drift by without meaning the stranger's rows were read. Generous
#: because this covers ten providers, several of which re-plan when the table statistics move.
ROW_TOLERANCE = 60

#: Statements one search may issue. A ratchet - see the module docstring.
STATEMENT_CEILING = 70

#: A bound whose id list comes back empty is answered without asking the database, so the count can
#: move by a statement or two as rows appear. That is not the defect this watches for; a count that
#: tracks a row count is.
SHORT_CIRCUIT_SLACK = 4

#: Matching pins the viewer gains, to make a query-per-result visible as one.
EXTRA_PINS = 12


class _WholePanelCase(TestCase):
    """A viewer with a little of everything searchable, and a stranger with a growable pile."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer_user = baker.make(User)
        self.viewer = self.viewer_user.profile
        self.stranger = baker.make(User).profile
        self.seeded = 0

        my_location = baker.make(Location, latitude=44.100001, longitude=-70.100001)
        self.mine = baker.make(Pin, profile=self.viewer, location=my_location, name=f"{TERM} pin", description="")
        baker.make(PinAlias, pin=self.mine, name=f"{TERM} alias")
        baker.make(PinNote, pin=self.mine, text=f"{TERM} note")

        self.my_trip = baker.make(Trip, creator=self.viewer, name=f"{TERM} trip", description="")
        baker.make(TripMembership, trip=self.my_trip, profile=self.viewer)
        baker.make(TripComment, trip=self.my_trip, author=self.viewer, text=f"{TERM} comment")

        their_location = baker.make(Location, latitude=44.200002, longitude=-70.200002)
        self.their_pin = baker.make(Pin, profile=self.stranger, location=their_location, name="Theirs", description="")
        self.their_trip = baker.make(Trip, creator=self.stranger, name="Their Trip", description="")
        baker.make(TripMembership, trip=self.their_trip, profile=self.stranger)
        self.grow_stranger(2)

    def grow_stranger(self, count: int, *, matching: bool = True) -> None:
        """Give the stranger *count* more of every row type the providers scan, then re-analyse."""
        word = TERM if matching else OTHER
        for _ in range(count):
            self.seeded += 1
            baker.make(PinAlias, pin=self.their_pin, name=f"{word} alias {self.seeded}")
            baker.make(PinNote, pin=self.their_pin, text=f"{word} note {self.seeded}")
            baker.make(TripComment, trip=self.their_trip, author=self.stranger, text=f"{word} comment {self.seeded}")
        tables = ", ".join(
            model._meta.db_table
            for model in (PinAlias, PinNote, TripComment, Pin, Trip)  # noqa: SLF001
        )
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {tables}")  # noqa: S608

    def grow_viewer(self, count: int) -> None:
        """Give the viewer *count* more matching pins, so an N+1 over results would show up."""
        for index in range(count):
            location = baker.make(Location, latitude=44.3 + index / 1000, longitude=-70.3 - index / 1000)
            baker.make(Pin, profile=self.viewer, location=location, name=f"{TERM} more {index}", description="")
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {Pin._meta.db_table}")  # noqa: S608

    def search(self) -> tuple[int, list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]]]:
        """Run one whole search as the panel does, capturing every statement it issued.

        The profile is re-fetched first because the wiki access scope memoises onto the Profile
        instance: reusing one would measure a warm cache the panel's own request never has.
        """
        captured: list[tuple[str, Sequence[Any] | Mapping[str, Any] | None]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        profile = Profile.objects.get(pk=self.viewer.pk)
        with connection.execute_wrapper(capture):
            response = GlobalSearchEngine().search(profile, TERM)
        return response.total, captured

    @staticmethod
    def readable(
        captured: Sequence[tuple[str, Sequence[Any] | Mapping[str, Any] | None]],
    ) -> list[tuple[int, str, Sequence[Any] | Mapping[str, Any] | None]]:
        """The captured statements ``EXPLAIN`` can be run against - the plain reads."""
        return [
            (i, sql, params) for i, (sql, params) in enumerate(captured) if sql.lstrip().upper().startswith("SELECT")
        ]

    def rows_read_searching(self) -> int:
        _, captured = self.search()
        statements = self.readable(captured)
        self.assertTrue(statements, "the search issued no readable statement, so nothing was measured")
        return sum(rows_examined(sql, params, preamble=session_preamble(captured, i)) for i, sql, params in statements)

    def statements_issued(self) -> int:
        _, captured = self.search()
        return len(captured)


class SearchReadsOnlyTheViewersRowsTests(_WholePanelCase):
    """Growing a stranger's data must not make the viewer's search read more of the database."""

    def test_rows_read_do_not_grow_with_a_strangers_matching_rows(self) -> None:
        self.assertRowsDoNotGrow(matching=True)

    def test_rows_read_do_not_grow_with_a_strangers_unrelated_rows(self) -> None:
        self.assertRowsDoNotGrow(matching=False)

    def assertRowsDoNotGrow(self, *, matching: bool) -> None:
        before = self.rows_read_searching()
        self.grow_stranger(GROWTH, matching=matching)
        after = self.rows_read_searching()
        if after > before + ROW_TOLERANCE:
            _, captured = self.search()
            worst = sorted(
                (
                    (rows_examined(sql, params, preamble=session_preamble(captured, i)), i)
                    for i, sql, params in self.readable(captured)
                ),
                reverse=True,
            )[:3]
            detail = [
                (rows, relations_read(captured[i][0], captured[i][1], preamble=session_preamble(captured, i)))
                for rows, i in worst
            ]
            kind = "matching" if matching else "non-matching"
            self.fail(
                f"one search read {before} rows, then {after} after a stranger added {GROWTH} {kind} rows of "
                f"their own - {after - before} more rows for data the viewer has never earned access to. "
                f"The three heaviest statements read: {detail}",
            )


class SearchCostsAFixedNumberOfStatementsTests(_WholePanelCase):
    """The statement count is a property of the provider chain, not of anybody's row count."""

    def test_it_stays_inside_its_ceiling(self) -> None:
        issued = self.statements_issued()
        self.assertLessEqual(
            issued,
            STATEMENT_CEILING,
            f"one search issued {issued} statements against a ceiling of {STATEMENT_CEILING}. Each one holds a "
            f"pooled connection, which is what bounds concurrent users once latency is inside budget.",
        )

    def test_it_does_not_grow_with_a_strangers_rows(self) -> None:
        before = self.statements_issued()
        self.grow_stranger(GROWTH)
        after = self.statements_issued()
        self.assertLessEqual(
            after - before,
            SHORT_CIRCUIT_SLACK,
            f"a stranger's {GROWTH} rows took the viewer's search from {before} statements to {after}",
        )

    def test_it_does_not_grow_with_the_viewers_own_results(self) -> None:
        before = self.statements_issued()
        self.grow_viewer(EXTRA_PINS)
        after = self.statements_issued()
        self.assertLess(
            after - before,
            EXTRA_PINS,
            f"the viewer's search issued {before} statements with one matching pin and {after} with "
            f"{EXTRA_PINS + 1}, which is a query per result rather than a query per provider",
        )


class TheMeasurementIsRealTests(_WholePanelCase):
    """Guards the tests above: if these fail, they are measuring nothing."""

    def test_the_search_finds_the_viewers_own_rows(self) -> None:
        total, _ = self.search()
        self.assertGreater(total, 0, "the viewer's search found nothing, so the measured statements are the empty path")

    def test_it_reads_the_tables_the_stranger_grows(self) -> None:
        _, captured = self.search()
        touched: set[str] = set()
        for i, sql, params in self.readable(captured):
            touched.update(relations_read(sql, params, preamble=session_preamble(captured, i)))
        for model in (PinAlias, PinNote, TripComment):
            self.assertIn(
                model._meta.db_table,  # noqa: SLF001
                touched,
                f"no plan read {model._meta.db_table}, so growing it proves nothing; relations read were {sorted(touched)}",  # noqa: SLF001
            )

    def test_the_stranger_is_not_visible_to_the_viewer(self) -> None:
        self.assertEqual(Pin.objects.filter(profile=self.viewer, pk=self.their_pin.pk).count(), 0)
        self.assertEqual(Trip.objects.filter(profiles=self.viewer, pk=self.their_trip.pk).count(), 0)
