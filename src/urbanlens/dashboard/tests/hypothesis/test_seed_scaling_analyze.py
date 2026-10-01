"""The scaling harness must tell the planner about the rows it just seeded."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.query_scaling import QueryScalingMixin
from urbanlens.core.tests.scaling import SeedScalingMixin
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.labels.model import Label


class WrittenTableExtractionTests(SimpleTestCase):
    """The table set comes from the statements, so it has to read them correctly."""

    def test_it_finds_the_target_of_each_kind_of_write(self) -> None:
        tables = SeedScalingMixin._written_tables(
            [
                'INSERT INTO "dashboard_locations" ("id", "name") VALUES (1, \'x\')',
                'UPDATE "dashboard_user_pins" SET "updated" = now() WHERE "id" = 3',
                'DELETE FROM "dashboard_labels" WHERE "id" = 7',
            ],
        )
        self.assertEqual(tables, {"dashboard_locations", "dashboard_user_pins", "dashboard_labels"})

    def test_it_ignores_reads(self) -> None:
        """A SELECT changes no row counts, so analysing its tables would be waste."""
        self.assertEqual(
            SeedScalingMixin._written_tables(['SELECT "id" FROM "dashboard_user_pins" WHERE "profile_id" = 1']),
            set(),
        )

    def test_it_reads_an_unquoted_table_name(self) -> None:
        """Django quotes identifiers, but the m2m and raw-SQL paths do not always."""
        self.assertEqual(
            SeedScalingMixin._written_tables(["insert into dashboard_user_pins_labels (pin_id) values (1)"]),
            {"dashboard_user_pins_labels"},
        )


class _RecordingScalingCase(QueryScalingMixin, TestCase):
    """A scaling case that records what it was asked to analyse."""

    analyzed: list[set[str]]

    def setUp(self) -> None:
        super().setUp()
        self.analyzed = []
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def analyze(self, tables: set[str]) -> None:
        self.analyzed.append(set(tables))
        super().analyze(tables)

    def seed_rows(self, count: int) -> None:
        for index in range(count):
            baker.make(Label, kind="tag", name=f"Analyze Probe {len(self.analyzed)}-{index}")

    def runTest(self) -> None:  # noqa: N802 - unittest's own spelling
        """Never collected as a test itself; `seed` is driven directly below."""


class SeedRefreshesStatisticsTests(TestCase):
    """`seed` must analyse what `seed_rows` wrote, and nothing else."""

    def _case(self) -> _RecordingScalingCase:
        case = _RecordingScalingCase()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_seeding_analyses_the_table_the_seed_wrote_to(self) -> None:
        case = self._case()

        case.seed(2)

        self.assertEqual(len(case.analyzed), 1, "seed() must analyse exactly once per call")
        self.assertIn(Label._meta.db_table, case.analyzed[0])

    def test_it_does_not_analyse_the_whole_database(self) -> None:
        """The measured reason the table list is derived rather than omitted."""
        case = self._case()

        case.seed(2)

        self.assertLess(
            len(case.analyzed[0]),
            20,
            f"seed() analysed {len(case.analyzed[0])} tables; it should name only what the seed wrote",
        )

    def test_extra_tables_are_included_for_writes_the_seed_cannot_see(self) -> None:
        """A trigger's table has no INSERT of its own in the captured SQL."""
        case = self._case()
        case.extra_analyzed_tables = ("dashboard_locations",)

        case.seed(1)

        self.assertIn("dashboard_locations", case.analyzed[0])

    def test_the_statistics_really_move(self) -> None:
        """The end-to-end claim: after seed(), the planner knows the rows are there.

        `pg_class.reltuples` is -1 on a table that has never been analysed, and a non-negative estimate
        afterwards."""
        case = self._case()

        case.seed(6)

        with connection.cursor() as cursor:
            cursor.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [Label._meta.db_table])
            reltuples = cursor.fetchone()[0]
        self.assertGreaterEqual(
            reltuples,
            0,
            "pg_class.reltuples is still -1, so ANALYZE never ran against the seeded table",
        )


class AnalyzeIsSafeToCallWithNothingTests(TestCase):
    """An empty target set must be a no-op, not a bare ANALYZE."""

    def test_no_tables_runs_no_statement(self) -> None:
        """A seed that wrote nothing must not fall back to the 1.70s whole-database form."""
        case = _RecordingScalingCase()
        case.setUp()
        self.addCleanup(case.doCleanups)

        with CaptureQueriesContext(connection) as captured:
            case.analyze(set())

        analyzed = [query["sql"] for query in captured.captured_queries if "ANALYZE" in query["sql"].upper()]
        self.assertEqual(analyzed, [], f"analyze(set()) still executed {analyzed}")

    def test_a_named_table_does_run_one(self) -> None:
        """The negative above is only meaningful if the positive also holds."""
        case = _RecordingScalingCase()
        case.setUp()
        self.addCleanup(case.doCleanups)

        with CaptureQueriesContext(connection) as captured:
            case.analyze({Label._meta.db_table})

        analyzed = [query["sql"] for query in captured.captured_queries if "ANALYZE" in query["sql"].upper()]
        self.assertEqual(len(analyzed), 1, f"expected exactly one ANALYZE, got {analyzed}")
        self.assertIn(Label._meta.db_table, analyzed[0])
