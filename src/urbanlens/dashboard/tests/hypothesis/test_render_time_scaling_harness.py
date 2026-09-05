"""The render-time harness must fail the page it exists to fail.

`QueryScalingMixin` has a harness test for the same reason: an instrument nobody
has watched move is worth as little as its readings. These check the three ways
`RenderTimeScalingMixin` earns its place - it passes a page whose rows are cheap,
fails one whose rows are not, and refuses a seed that does not change what the
page renders.

The last one matters most, and is inherited rather than reimplemented: it is the
same guard that caught a 2026-08-17 survey reporting the conversation list "flat"
while seeding pins it never listed.
"""

from __future__ import annotations

from django.test import override_settings
from model_bakery import baker
import pytest

from urbanlens.core.tests.query_scaling import QueryScalingMixin
from urbanlens.core.tests.render_scaling import RenderTimeScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement

_URLCONF = "urbanlens.dashboard.tests.urls_render_scaling"


class _AchievementSeedMixin:
    """Both pages list every achievement, so both seed the same way."""

    def seed_rows(self, count: int) -> None:
        """Create *count* more achievements.

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
    """The argument for this mixin existing at all, in executable form.

    The expensive page reads its rows in one query however many there are, so it
    is flat by every measure the query mixin has - and it is the page the render
    mixin above refuses.
    """

    def test_the_expensive_page_is_perfectly_flat_in_queries(self) -> None:
        self.assert_flat("/expensive/")


@override_settings(ROOT_URLCONF=_URLCONF)
class PreSeededBaselineTests(_AchievementSeedMixin, RenderTimeScalingMixin, TestCase):
    """Seeding in `setUp` silently disarms this measurement, and cannot be caught.

    The baseline is the denominator, so rows already on the page inflate it. The
    icon-grid page measures 4.1-5.8 baselines per row from empty and 0.070-0.083
    with a dozen rows already rendered - a collapse of roughly sixty-fold, and
    the difference between failing by 40x and passing.

    It is pinned rather than fixed because it is not detectable from the outside:
    a baseline taken at `n0` rows yields `k` and `C + k*n0`, and nothing separates
    `C` from `n0`. So the rule is a rule about how to write the subclass, and this
    is what keeps it honest.

    The budget here is explicit rather than the default 10%, deliberately. The
    pre-seeded reading sits only 17-30% under that default, which is close enough
    to it that host load could tip this test red and send someone hunting a
    regression that is really this documented limitation flickering. What the test
    is for is the collapse, not the last few percent of it.
    """

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
