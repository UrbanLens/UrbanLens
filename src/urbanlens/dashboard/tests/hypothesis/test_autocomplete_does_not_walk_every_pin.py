"""Map autocomplete must read the viewer's pins, not every pin on the site.

Measured on the capacity population (471,756 pins, X28): one keystroke cost 405 ms of SQL, and
`EXPLAIN (ANALYZE, BUFFERS)` put 197 ms of it in a single node:

    Index Scan using dashboard_user_pins_pkey on dashboard_user_pins
      (actual time=20.942..197.009 rows=1859 loops=1)
      Filter: (profile_id = 4)
      Rows Removed by Filter: 500199

The viewer had 1,859 pins and the plan read 502,058. `search_local` ends in `.distinct()`, whose
`Unique` node wants its input sorted by the pin's primary key, and the cheapest presorted source
the planner had was the primary key itself - so it walked the table in id order and threw away
every row belonging to somebody else. The existing `(profile_id, ...)` indexes all sort by
something the `Unique` cannot use.

The axis is rows *read*. The query returns at most 12 either way, so a query counter, a row-count
wrapper and a latency budget all read this as one slow query rather than as a plan whose cost is
set by how many pins *other people* have. That is what makes it a capacity defect and not a
latency one: every account's autocomplete got slower each time any account added pins.

A plan-based test inherits the planner's cost estimates, which are not scoped to one test's
transaction - see `urbanlens.core.tests.explain`. Treat a failure here as inconclusive until it
reproduces with this file run alone.

**The second cost this file guards is planning, not execution.** With the index in place the same
keystroke cost 233 ms to plan and 52 ms to run, and the planning was bought almost entirely by
`select_related` on the *filtering* query: measured on the same population, dropping it took
planning from 215.7 ms to 6.7 ms. Ten relations with two hundred output columns is an expensive
join order to search for, and the search is repeated per keystroke because a plan is only reused
once psycopg has prepared the statement and Postgres has stopped building custom plans for it.
Selecting ids first and fetching those twelve rows by primary key costs 4.4 + 1.0 ms to plan:
292.8 ms of database time a keystroke becomes 44.3.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.explain import relations_read
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.map_pins.autocomplete import search_local

#: What the viewer types. Two characters is the shortest `search_local` will act on.
TERM = "quokka"

#: The viewer's own pins. Small enough that reading a multiple of it is still unmistakably
#: "the viewer's pins" rather than "the table".
MINE = 200

#: Pins belonging to someone the viewer cannot see. The planner picks its scan by cost, so this
#: has to be large enough that walking the table in primary-key order actually looks cheaper than
#: sorting - at a thousand rows it does not, and the test passes whatever the schema says.
THEIRS = 30000


def _select_list(sql: str) -> str:
    """The columns a statement returns, without the clauses that only mention them.

    Args:
        sql: A statement, as executed.

    Returns:
        Everything between the leading ``SELECT`` and its ``FROM``.
    """
    return sql[: sql.find(" FROM ")] if " FROM " in sql else sql


class AutocompleteReadsOneAccountsPinsTests(TestCase):
    """A viewer with a few pins, on a site where somebody else has many."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin, which grants every feature
        self.viewer = baker.make(User).profile
        self.stranger = baker.make(User).profile
        self.placed = 0

        self._pins(self.viewer, MINE)
        self._pins(self.stranger, THEIRS)
        with connection.cursor() as cursor:
            cursor.execute(f"ANALYZE {Pin._meta.db_table}, {Location._meta.db_table}")  # noqa: S608 - names from the ORM

    def _pins(self, profile: Profile, count: int) -> None:
        """Give *profile* *count* pins, each at its own coordinates.

        Locations are unique on (latitude, longitude), so every pin needs its own. Built with
        `bulk_create` because the row count this test needs is set by the planner rather than by
        the assertion, and seeding it one `save()` at a time takes minutes.

        Args:
            profile: Who owns them.
            count: How many to make.
        """
        locations = []
        for _ in range(count):
            self.placed += 1
            locations.append(
                Location(latitude=f"44.{self.placed:06d}", longitude=f"-70.{self.placed:06d}"),
            )
        Location.objects.bulk_create(locations, batch_size=2000)
        Pin.objects.bulk_create(
            [
                Pin(profile=profile, location=location, name=f"Spot {index}", description="")
                for index, location in enumerate(locations)
            ],
            batch_size=2000,
        )

    def _statements(self) -> list[tuple[str, object]]:
        """Every statement one keystroke issues, in order."""
        captured: list[tuple[str, object]] = []

        def capture(execute, sql, params, many, context):  # noqa: ANN001, ANN202
            captured.append((sql, params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(capture):
            search_local(TERM, self.viewer)
        return captured

    def _pin_rows_read(self) -> int:
        """Rows the pin search reads out of the pin table, counting rows a filter discarded.

        Planned with ``enable_sort = off``, which is what makes this measurable at a scale a test
        can seed. The production plan was chosen because the ``Unique`` wanted presorted input and
        the only presorted source was the primary key; at 30,000 rows a plain sort is cheap enough
        that the planner takes it and the defect hides. Taking the sort away reproduces the same
        choice - the plan must find an index whose order the ``Unique`` can use - so the test asks
        the question production asked, rather than waiting for half a million rows to ask it.
        """
        table = Pin._meta.db_table
        searching = [(sql, params) for sql, params in self._statements() if table in sql and "DISTINCT" in sql]
        self.assertTrue(searching, "no statement in the capture searched the pin table")
        preamble = [("SET enable_sort = off", None)]
        return max(relations_read(sql, params, preamble=preamble).get(table, 0) for sql, params in searching)

    def test_it_does_not_read_a_strangers_pins(self) -> None:
        """The cost of a keystroke belongs to the viewer's own pin count, not to the site's."""
        read = self._pin_rows_read()

        self.assertLess(
            read,
            MINE * 4,
            f"autocomplete read {read} pin rows for a viewer who owns {MINE}, on a site holding "
            f"{MINE + THEIRS + 1}: the plan is walking the table and discarding other accounts' pins",
        )

    def test_the_statement_that_scans_selects_one_column(self) -> None:
        """What the scanning query returns is what sets its planning cost.

        `select_related` on the filtering query puts four tables' columns in a select list the
        query then has to `DISTINCT` over, and the planner searches join orders for all of them
        on every keystroke. Fetching the twelve matched rows in a second statement keyed by
        primary key costs 1 ms to plan; carrying them through the scan costs 210.

        The filtering statement is the one joining the pin alias table - the term itself is a
        bound parameter, so it never appears in the SQL text to match on. Only its select list
        is examined: the filter legitimately *mentions* the wiki's description, in the `WHERE`.
        """
        aliases = Pin.aliases.rel.related_model._meta.db_table
        wide = [
            sql
            for sql, _ in self._statements()
            if f'"{aliases}"' in sql and f'"{Wiki._meta.db_table}"."description"' in _select_list(sql)
        ]

        self.assertEqual(
            wide,
            [],
            "the statement applying the text filter also selects the joined tables' columns, "
            "which is the shape whose join order the planner re-searches on every keystroke",
        )
