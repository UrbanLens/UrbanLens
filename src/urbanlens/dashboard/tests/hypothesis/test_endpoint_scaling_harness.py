"""The endpoint mixin must fail each of the endpoints it exists to fail.

An instrument nobody has watched move is worth as little as its readings, and
this one adds two axes that no existing mixin covers - so each has to be shown
failing a view that really is broken that way, and passing one that is not.

The negative half matters as much as the positive: a `bounded/` view that trips
any axis would mean the budgets are set below normal behaviour, and every gate
built on this would be noise.
"""

from __future__ import annotations

import json
from typing import Any

from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import override_settings
from model_bakery import baker
import pytest

from urbanlens.core.tests.endpoint_scaling import EndpointScalingMixin
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.tests.urls_endpoint_scaling import PAYLOAD_CEILING

_URLCONF = "urbanlens.dashboard.tests.urls_endpoint_scaling"


class _AchievementSeedMixin(EndpointScalingMixin):
    """Every view here lists achievements, so they all seed the same way."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def seed_rows(self, count: int) -> None:
        """Create *count* more achievements.

        Args:
            count: How many rows to add.
        """
        existing = Achievement.objects.count()
        for index in range(count):
            baker.make(
                Achievement,
                name=f"Endpoint Award {existing + index}",
                metric="pins_created",
                threshold=existing + index + 1,
            )

    def count_payload_rows(self, response: HttpResponse) -> int | None:
        """Read the record count out of the JSON body.

        Args:
            response: The response to read.

        Returns:
            How many records it carries, or None when the body is not the
            expected shape.
        """
        payload: Any = json.loads(response.content)
        rows = payload.get("rows")
        return len(rows) if isinstance(rows, list) else None


@override_settings(ROOT_URLCONF=_URLCONF)
class ABoundedEndpointPassesTests(_AchievementSeedMixin, TestCase):
    """The negative control: normal behaviour must not trip any budget."""

    def test_a_projection_endpoint_is_within_every_budget(self) -> None:
        self.assert_endpoint_scaling("/bounded/")


@override_settings(ROOT_URLCONF=_URLCONF)
class ObjectsPerRowIsCaughtTests(_AchievementSeedMixin, TestCase):
    """The axis R27 needed: one model instance per row of output."""

    def test_building_a_model_per_row_fails(self) -> None:
        with pytest.raises(AssertionError, match="objects/row"):
            self.assert_endpoint_scaling("/objects/")

    def test_the_failure_says_a_query_counter_cannot_see_it(self) -> None:
        """The sentence that stops the next reader trusting a flat query count."""
        with pytest.raises(AssertionError, match="a query counter reads this as flat"):
            self.assert_endpoint_scaling("/objects/")


@override_settings(ROOT_URLCONF=_URLCONF)
class RowsFetchedPerRowIsCaughtTests(_AchievementSeedMixin, TestCase):
    """The new axis: one statement, every row of the table, constant body.

    The budget that catches this is the *capped* one. A view reading one row per
    row is healthy when it renders them and pathological when it does not, and
    the number alone cannot tell those apart - which is why the budget is chosen
    from `expect_growth` rather than being a single constant.
    """

    def test_materialising_every_row_fails(self) -> None:
        with pytest.raises(AssertionError, match="rows fetched/row"):
            self.assert_endpoint_scaling(
                "/rows/",
                expect_growth=False,
                growth_waiver="the view returns a count, so its body is the same size at both sizes",
            )

    def test_the_same_reading_is_fine_when_the_rows_are_rendered(self) -> None:
        """The negative control for the axis, not just for the fixture.

        `/bounded/` reads exactly the same one row per row and is not a defect,
        because it renders them. If this ever fails, the capped budget has been
        applied where the growing one belongs and every list endpoint is about to
        start failing.
        """
        self.assert_endpoint_scaling("/bounded/")

    def test_the_query_count_really_is_flat_for_it(self) -> None:
        """Proves the axis is necessary, not merely available.

        If a statement counter caught this, the new axis would be redundant. It
        does not: there is one query at both sizes.
        """
        self.seed(self.first_batch)
        small = self.measure_queries("/rows/")
        self.seed(self.second_batch)
        large = self.measure_queries("/rows/")

        self.assertEqual(
            len(small),
            len(large),
            "the query count moved, so this view no longer demonstrates the gap the axis fills",
        )


@override_settings(ROOT_URLCONF=_URLCONF)
class BytesPerRowIsCaughtTests(_AchievementSeedMixin, TestCase):
    """An endpoint can be flat on every other axis and still be too fat."""

    def test_shipping_kilobytes_per_row_fails(self) -> None:
        with pytest.raises(AssertionError, match="bytes/row"):
            self.assert_endpoint_scaling("/bytes/")


@override_settings(ROOT_URLCONF=_URLCONF)
class ThePayloadCeilingIsCheckedTests(_AchievementSeedMixin, TestCase):
    """A per-row budget cannot express "and never more than N records"."""

    first_batch = 2
    second_batch = 6

    def test_an_uncapped_endpoint_fails_its_ceiling(self) -> None:
        with pytest.raises(AssertionError, match="against a ceiling of"):
            self.assert_endpoint_scaling("/uncapped/", payload_ceiling=PAYLOAD_CEILING)

    def test_a_capped_endpoint_passes(self) -> None:
        self.assert_endpoint_scaling(
            "/capped/",
            payload_ceiling=PAYLOAD_CEILING,
            expect_growth=False,
            growth_waiver="the view caps what it returns, so its body stops growing at the ceiling",
        )

    def test_asking_for_a_ceiling_without_a_counter_is_an_error_not_a_pass(self) -> None:
        """A silently-skipped assertion is the failure mode this guards.

        `count_payload_rows` returns None by default, so a case that asks for a
        ceiling and forgets to implement it would otherwise pass while checking
        nothing at all.
        """

        class _NoCounter(ABoundedEndpointPassesTests):
            def count_payload_rows(self, response: HttpResponse) -> int | None:
                return None

        case = _NoCounter("test_a_projection_endpoint_is_within_every_budget")
        case.setUp()
        self.addCleanup(case.doCleanups)

        with pytest.raises(AssertionError, match="count_payload_rows returned None"):
            case.assert_endpoint_scaling("/bounded/", payload_ceiling=PAYLOAD_CEILING)


@override_settings(ROOT_URLCONF=_URLCONF)
class EveryFailingAxisIsReportedTests(_AchievementSeedMixin, TestCase):
    """One run should say everything that is wrong, not the first thing."""

    def test_a_view_breaking_two_axes_reports_both(self) -> None:
        """`/objects/` builds an instance per row *and* reads a row per row.

        Reported together because fixing one at a time means measuring three
        times, and the second failure is usually a consequence of the first.
        """
        with pytest.raises(AssertionError) as caught:
            self.assert_endpoint_scaling("/objects/", max_rows_fetched_per_row=0.5)

        message = str(caught.value)
        self.assertIn("objects/row", message)
        self.assertIn("rows fetched/row", message)
