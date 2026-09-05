"""A notification title built from user text must fit the column it is stored in.

``NotificationLog.title`` is ``CharField(max_length=255)``, and most titles wrap
a user-controlled name in fixed text. Where that name's own column is *also*
255, the wrapper text is pure overflow: Postgres rejects the row with
``DataError``, Django does not truncate on the way in, and the write is not the
caller's own so nothing local suggests it can fail.

The instance that motivated this:

    title=f"Safety check-in posted to {wiki.name}"      # 26 + Wiki.name(255)

``post_checkin_to_community_wiki`` runs at the *top* of ``escalate_checkin``,
before the loop that reaches the emergency contacts. So a check-in whose
destination wiki has a long name did not merely lose its community post - the
``DataError`` aborted the escalation before a single emergency contact was told
the person was overdue. Child wiki names come straight from a request body
(``detail_pins`` child-wiki creation), so the 255 is reachable, not theoretical.

The fix truncates at the model, not at that one call site: ``title`` is written
from 20-odd places and a title is display text, where a clipped string beats a
failed write every time. The last test is the completeness arm - it holds the
property for every current call site instead of the one that was found.
"""

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

        Each pair is (wrapper text length, the column feeding it). Any pair
        whose sum exceeds the title column relies on truncation; this asserts
        the model provides it, so a new title with the same shape is safe.
        """
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
