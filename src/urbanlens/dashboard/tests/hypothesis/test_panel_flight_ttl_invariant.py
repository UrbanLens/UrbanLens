"""The panel single-flight marker must outlive the task it guards."""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.pins.external_data import FLIGHT_TTL_SECONDS
from urbanlens.dashboard.tasks import fetch_panel_source


class PanelFlightTtlInvariantTests(SimpleTestCase):
    """`FLIGHT_TTL_SECONDS` and `fetch_panel_source`'s limits have to move together."""

    def test_the_marker_outlives_a_hard_killed_task(self) -> None:
        self.assertIsNotNone(fetch_panel_source.time_limit, "fetch_panel_source lost its hard time limit")
        self.assertGreater(
            FLIGHT_TTL_SECONDS,
            fetch_panel_source.time_limit,
            "FLIGHT_TTL_SECONDS must exceed fetch_panel_source's hard time_limit, or a hard-killed "
            "task's single-flight marker expires while a duplicate fetch is still running.",
        )

    def test_the_soft_limit_leaves_room_to_clean_up(self) -> None:
        """The soft limit must fire early enough for the `finally` to release the marker."""
        self.assertIsNotNone(fetch_panel_source.soft_time_limit, "fetch_panel_source lost its soft time limit")
        self.assertLess(
            fetch_panel_source.soft_time_limit,
            fetch_panel_source.time_limit,
            "soft_time_limit must be under time_limit so SoftTimeLimitExceeded is catchable.",
        )
