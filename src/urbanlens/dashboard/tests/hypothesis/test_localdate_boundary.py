""""Today" is the user's today, not the server's.

``USE_TZ`` is on, so ``datetime.date.today()`` reads the *server's* timezone while
``django.utils.timezone.localdate()`` reads the *active* one. They disagree for part of
every day, and nine call sites used the former.

The consequential one is completing a trip activity, which computes
``effective_date = min(completed_date, today)`` - so at 23:00 in a timezone ahead of the
server's, "today" resolved to yesterday, clamping the effective date backwards and feeding
that date to the visit entries created for the activity.

**On vacuity.** A timezone test passes trivially if the two zones happen to agree at the
moment it runs, and a test that cannot fail is worse than no test. Every test here first
asserts that its frozen instant actually straddles a date boundary between the two zones -
so a setup that stops exercising the bug fails loudly instead of going quietly green.
"""

from __future__ import annotations

import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import override_settings
from django.utils import timezone

from urbanlens.core.tests.testcase import SimpleTestCase

#: 2026-08-14 23:30 UTC. In UTC it is the 14th; in Pacific/Auckland (UTC+12) it is
#: already the 15th. Any pair of zones straddling the instant would do.
_INSTANT = datetime.datetime(2026, 8, 14, 23, 30, tzinfo=datetime.UTC)


class LocaldateBoundaryTests(SimpleTestCase):
    def _frozen_now(self):
        return patch("django.utils.timezone.now", return_value=_INSTANT)

    def test_the_chosen_instant_really_does_straddle_a_boundary(self) -> None:
        """Precondition: without this, every other test here could pass vacuously."""
        with self._frozen_now():
            with override_settings(TIME_ZONE="UTC", USE_TZ=True):
                timezone.deactivate()
                server_side = timezone.localdate()
            with override_settings(TIME_ZONE="Pacific/Auckland", USE_TZ=True):
                timezone.deactivate()
                other_side = timezone.localdate()

        self.assertNotEqual(
            server_side,
            other_side,
            "the frozen instant no longer straddles a date boundary - this suite would pass vacuously",
        )

    def test_localdate_follows_the_active_timezone(self) -> None:
        with self._frozen_now():
            with override_settings(TIME_ZONE="UTC", USE_TZ=True):
                timezone.deactivate()
                self.assertEqual(timezone.localdate(), datetime.date(2026, 8, 14))

            with override_settings(TIME_ZONE="Pacific/Auckland", USE_TZ=True):
                timezone.deactivate()
                self.assertEqual(timezone.localdate(), datetime.date(2026, 8, 15))

    def test_date_today_does_not_follow_the_active_timezone(self) -> None:
        """The behaviour the nine call sites had, pinned so the distinction stays visible.

        The axis is ``timezone.activate()`` - the per-request zone the locale middleware sets -
        not ``override_settings(TIME_ZONE=...)``. Django's test signal for ``TIME_ZONE`` also
        rewrites ``os.environ["TZ"]`` and calls ``time.tzset()``, so under that override
        ``date.today()`` moves too and the distinction disappears. Activating a zone changes
        only Django's notion of "local", which is exactly what the buggy call sites ignored.

        Asserts *insensitivity* rather than a specific date, so it does not depend on the zone
        of the machine running it.
        """
        with self._frozen_now():
            timezone.deactivate()
            default_localdate = timezone.localdate()
            default_today = datetime.date.today()

            timezone.activate(ZoneInfo("Pacific/Auckland"))
            try:
                activated_localdate = timezone.localdate()
                activated_today = datetime.date.today()
            finally:
                timezone.deactivate()

        self.assertNotEqual(default_localdate, activated_localdate, "localdate() ignored the activated timezone")
        self.assertEqual(default_today, activated_today, "date.today() unexpectedly tracked the activated timezone")
