"""The render-time harness must fail the page it exists to fail."""

from __future__ import annotations

from unittest import mock

from django.test import override_settings
from model_bakery import baker
import pytest

from urbanlens.core.tests.query_scaling import QueryScalingMixin
from urbanlens.core.tests.render_scaling import REMEASURE_ATTEMPTS, RenderSample, RenderTimeScalingMixin
from urbanlens.core.tests.scaling import SeedScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement

_URLCONF = "urbanlens.dashboard.tests.urls_render_scaling"


class _AchievementSeedMixin(SeedScalingMixin):
    """Both pages list every achievement, so both seed the same way."""

    def seed_rows(self, count: int) -> None:
        """
        Create *count* more achievements.

        Args:
            count: How many rows to add.
        """
        existing = Achievement.objects.count()
        for index in range(count):
            baker.make(
                Achievement, name=f"Award {existing + index}", metric="pins_created", threshold=existing + index + 1
            )


@override_settings(ROOT_URLCONF=_URLCONF)
class CheapRowsPassTests(_AchievementSeedMixin, RenderTimeScalingMixin, TestCase):
    """A row of two spans against a page of two thousand elements."""

    def test_an_ordinary_list_page_is_within_budget(self) -> None:
        self.assert_row_cost_bounded("/cheap/")


@override_settings(ROOT_URLCONF=_URLCONF)
class ExpensiveRowsFailTests(_AchievementSeedMixin, RenderTimeScalingMixin, TestCase):
    """A row that renders a full icon grid - the shape this was built for."""

    def test_a_row_that_renders_an_icon_grid_is_reported(self) -> None:
        with pytest.raises(AssertionError, match="of render time per extra row"):
            self.assert_row_cost_bounded("/expensive/")

    def test_the_failure_says_what_to_do_about_it(self) -> None:
        """The message has to end the investigation, not start one."""
        with pytest.raises(AssertionError) as raised:
            self.assert_row_cost_bounded("/expensive/")

        message = str(raised.value)
        self.assertIn("per row", message, "the bytes-per-row line is what names the fix on sight")
        self.assertIn("queries", message, "without the query count the number cannot be interpreted")
        self.assertIn("empty render", message)

    def test_a_deliberate_budget_can_be_raised(self) -> None:
        """An expensive row is sometimes the point; the exemption must be visible."""
        self.assert_row_cost_bounded("/expensive/", max_row_cost=100.0)


@override_settings(ROOT_URLCONF=_URLCONF)
class QueryCountCannotSeeItTests(_AchievementSeedMixin, QueryScalingMixin, TestCase):
    """
    The argument for this mixin existing at all, in executable form.

    The expensive page reads its rows in one query however many there are, so it is flat by every measure the
    query mixin has - and it is the page the render mixin above refuses."""

    def test_the_expensive_page_is_perfectly_flat_in_queries(self) -> None:
        self.assert_flat("/expensive/")


@override_settings(ROOT_URLCONF=_URLCONF)
class PreSeededBaselineTests(_AchievementSeedMixin, RenderTimeScalingMixin, TestCase):
    """
    Seeding in `setUp` silently disarms this measurement, and cannot be caught.

    The icon-grid page measures 4.1-5.8 baselines per row from empty and 0.070-0.083 with a dozen rows already
    rendered - a collapse of roughly sixty-fold, and the difference between failing by 40x and passing."""

    def setUp(self) -> None:
        super().setUp()
        self.seed_rows(12)

    def test_pre_seeded_rows_hide_a_page_that_would_otherwise_fail(self) -> None:
        self.assert_row_cost_bounded("/expensive/", max_row_cost=0.5)


@override_settings(ROOT_URLCONF=_URLCONF)
class SeedThatDoesNothingTests(RenderTimeScalingMixin, TestCase):
    """A seed that grows nothing must fail, not pass."""

    def seed_rows(self, count: int) -> None:
        """Deliberately create nothing - the mistake under test."""

    def test_an_unexercised_endpoint_is_reported_rather_than_passed(self) -> None:
        with pytest.raises(AssertionError, match="does not exercise this endpoint"):
            self.assert_row_cost_bounded("/cheap/")

    def test_waiving_growth_without_a_reason_is_refused(self) -> None:
        with pytest.raises(AssertionError, match="growth_waiver"):
            self.assert_row_cost_bounded("/cheap/", expect_growth=False)


def _sample(ms: float, body_bytes: int) -> RenderSample:
    return RenderSample(best_ms=ms, samples_ms=(ms,), body_bytes=body_bytes)


#: One attempt's three timings (baseline, small, large). Per-row cost is (large - small) / 9, over a 10 ms baseline.
_QUIET = [_sample(10.0, 1000), _sample(10.5, 1300), _sample(11.0, 2200)]  # 0.06 ms/row = 0.6% of the page
_OUTLIER = [_sample(10.0, 1000), _sample(10.5, 1300), _sample(21.0, 2200)]  # 1.17 ms/row = 12% of the page
_EXPENSIVE = [_sample(10.0, 1000), _sample(11.0, 1300), _sample(60.0, 2200)]  # 5.4 ms/row = 54% of the page


@override_settings(ROOT_URLCONF=_URLCONF)
class RemeasureOutlierTests(_AchievementSeedMixin, RenderTimeScalingMixin, TestCase):
    """A noise outlier must not fail the build; a consistent overrun must. Timing is mocked, so nothing sleeps."""

    def _run(self, attempts: list[list[RenderSample]]) -> mock.Mock:
        timings = mock.Mock(side_effect=[sample for attempt in attempts for sample in attempt])
        with mock.patch.object(self, "time_request", timings):
            self.assert_row_cost_bounded("/cheap/")
        return timings

    def test_a_transient_outlier_passes_when_a_remeasurement_is_within_budget(self) -> None:
        timings = self._run([_OUTLIER, _QUIET])
        self.assertEqual(timings.call_count, 6, "the over-budget attempt is measured again, then stops")

    def test_a_second_outlier_still_passes_on_the_last_attempt(self) -> None:
        timings = self._run([_OUTLIER, _OUTLIER, _QUIET])
        self.assertEqual(timings.call_count, 9)

    def test_a_consistent_overrun_fails_and_reports_every_attempt(self) -> None:
        attempts = [_EXPENSIVE] * (1 + REMEASURE_ATTEMPTS)
        with pytest.raises(AssertionError) as raised:
            self._run(attempts)
        message = str(raised.value)
        self.assertIn(f"all {1 + REMEASURE_ATTEMPTS} attempts", message)
        self.assertIn("54%, 54%, 54%", message)

    def test_an_in_budget_page_is_measured_once(self) -> None:
        timings = self._run([_QUIET])
        self.assertEqual(timings.call_count, 3)

    def test_remeasuring_starts_from_zero_rows_each_time(self) -> None:
        """The per-row arithmetic divides by second_batch, so each attempt must reseed from nothing."""
        counts: list[int] = []
        real_seed = self.seed_rows

        def counting_seed(count: int) -> None:
            counts.append(Achievement.objects.count())
            real_seed(count)

        with mock.patch.object(self, "seed_rows", counting_seed):
            self._run([_OUTLIER, _QUIET])
        self.assertEqual(counts, [0, self.first_batch, 0, self.first_batch])
