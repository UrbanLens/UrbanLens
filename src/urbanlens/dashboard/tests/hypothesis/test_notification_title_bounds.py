"""A notification title built from user text must fit the column it is stored in."""

from __future__ import annotations

import datetime

from django.utils import timezone
from model_bakery import baker

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.achievements.model import Achievement
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.visits.safety import escalate_checkin

_TITLE_MAX = NotificationLog._meta.get_field("title").max_length


class NotificationTitleTruncationTests(TestCase):
    def test_an_overlong_title_is_stored_clipped_rather_than_rejected(self) -> None:
        log = NotificationLog.objects.create(profile=baker.make("auth.User").profile, title="x" * (_TITLE_MAX + 50))

        log.refresh_from_db()
        self.assertEqual(len(log.title), _TITLE_MAX)

    @settings(deadline=None, max_examples=25)
    @given(st.integers(min_value=0, max_value=600))
    def test_no_title_length_can_fail_the_write(self, length: int) -> None:
        log = NotificationLog.objects.create(profile=baker.make("auth.User").profile, title="t" * length)

        self.assertEqual(log.title, "t" * min(length, _TITLE_MAX))

    def test_a_title_that_already_fits_is_untouched(self) -> None:
        """Truncation must not be doing anything to the ordinary case."""
        log = NotificationLog.objects.create(
            profile=baker.make("auth.User").profile, title="Someone replied to your comment"
        )

        log.refresh_from_db()
        self.assertEqual(log.title, "Someone replied to your comment")


class EscalationSurvivesLongWikiNameTests(TestCase):
    """The reported shape: the escalation must still reach the contacts."""

    def test_a_maximal_wiki_name_does_not_abort_the_escalation(self) -> None:
        owner = baker.make("auth.User", email="owner@example.com").profile
        location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        baker.make(Wiki, location=location, name="W" * Wiki._meta.get_field("name").max_length)
        baker.make("dashboard.Pin", profile=baker.make("auth.User", email="pin@example.com").profile, location=location)
        checkin = baker.make(
            SafetyCheckin,
            profile=owner,
            title="Overdue hike",
            checkin_by=timezone.now() - datetime.timedelta(hours=2),
            grace_period=datetime.timedelta(hours=1),
            destination_latitude="40.0",
            destination_longitude="-74.0",
            status=SafetyCheckinStatus.AWAITING_CHECKIN,
            notify_community_wiki=True,
        )
        contact = baker.make("dashboard.SafetyCheckinContact", checkin=checkin, email="contact@example.com")

        escalate_checkin(checkin)

        contact.refresh_from_db()
        self.assertIsNotNone(
            contact.notified_at, "the wiki post aborted the escalation before the emergency contacts were reached"
        )

    # -- completeness -------------------------------------------------------

    def test_every_name_a_title_wraps_still_leaves_room_or_is_truncated(self) -> None:
        """The property, held against the models rather than against today's call sites.

        Each pair is (wrapper text length, the column feeding it)."""
        wrapped = {
            "safety wiki post": (len("Safety check-in posted to "), Wiki._meta.get_field("name").max_length),
            "achievement": (len("Achievement unlocked: "), Achievement._meta.get_field("name").max_length),
        }

        for label, (wrapper, source_max) in wrapped.items():
            with self.subTest(title=label):
                log = NotificationLog.objects.create(
                    profile=baker.make("auth.User").profile, title="w" * wrapper + "n" * source_max
                )

                log.refresh_from_db()
                self.assertLessEqual(len(log.title), _TITLE_MAX)
