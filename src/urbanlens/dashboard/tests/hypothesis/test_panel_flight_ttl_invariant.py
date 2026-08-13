"""The panel single-flight marker must outlive the task it guards.

Panel fetches are single-flight: ``schedule_panel_fetch`` claims a cache key with
an atomic ``cache.add`` and ``fetch_panel_source`` releases it in a ``finally``.
A task the worker *hard-kills* never reaches that ``finally``, so the marker's
TTL is the only thing that frees the panel — hence
``FLIGHT_TTL_SECONDS > time_limit``.

Both sides document the relationship (``external_data.FLIGHT_TTL_SECONDS``'s
comment and the one above ``fetch_panel_source``), but the numbers are literals
in two different modules and nothing checked that they still agree. Raise the
task's ``time_limit`` past the TTL and the marker expires *while the task is
still running*: the next poll re-claims it and enqueues a duplicate fetch, so a
slow provider gets concurrent duplicate requests from every polling page —
precisely what single-flight exists to prevent, and it degrades silently, as
extra API spend rather than an error.
"""

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
