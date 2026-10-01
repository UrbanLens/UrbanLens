"""The scaling harness must fail the tests it exists to fail."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.query_scaling import QueryScalingMixin, normalize_sql, queries_that_grew
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase


class SeedThatDoesNothingTests(QueryScalingMixin, TestCase):
    """A seed that grows nothing must fail, not pass."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Deliberately create nothing - the mistake under test."""

    def test_an_unexercised_endpoint_is_reported_rather_than_passed(self) -> None:
        with pytest.raises(AssertionError, match="does not exercise this endpoint"):
            self.assert_flat(reverse("trips.overview"))

    def test_the_growth_requirement_can_be_waived_with_a_reason(self) -> None:
        """Paginated endpoints cap what they render; the waiver must be explicit."""
        self.assert_flat(
            reverse("trips.overview"), expect_growth=False, growth_waiver="nothing is seeded in this harness test"
        )

    def test_waiving_growth_without_a_reason_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="growth_waiver"):
            self.assert_flat(reverse("trips.overview"), expect_growth=False)

    def test_a_subclass_must_implement_its_seed(self) -> None:
        """The base implementation refuses rather than silently seeding nothing."""

        class Unseeded(QueryScalingMixin):
            pass

        with pytest.raises(NotImplementedError):
            Unseeded().seed_rows(1)


class GrowthReportingTests(SimpleTestCase):
    """The failure message has to name the statement that multiplied."""

    def test_normalisation_collapses_differing_literals(self) -> None:
        first = normalize_sql("SELECT * FROM pins WHERE id = 12 AND name = 'mill'")
        second = normalize_sql("SELECT * FROM pins WHERE id = 4098 AND name = 'other'")

        self.assertEqual(first, second)

    def test_only_statements_that_grew_are_reported_and_ranked(self) -> None:
        before = [{"sql": "SELECT a FROM t WHERE id = 1"}, {"sql": "SELECT b FROM u"}]
        after = [
            {"sql": "SELECT a FROM t WHERE id = 1"},
            {"sql": "SELECT a FROM t WHERE id = 2"},
            {"sql": "SELECT a FROM t WHERE id = 3"},
            {"sql": "SELECT b FROM u"},
        ]

        grown = queries_that_grew(before, after)

        self.assertEqual(len(grown), 1, "only the per-row statement should be reported")
        small, large, sql = grown[0]
        self.assertEqual((small, large), (1, 3))
        self.assertIn("FROM t", sql)

    def test_nothing_is_reported_when_the_shape_is_identical(self) -> None:
        queries = [{"sql": "SELECT a FROM t WHERE id = 7"}]

        self.assertEqual(queries_that_grew(queries, queries), [])
