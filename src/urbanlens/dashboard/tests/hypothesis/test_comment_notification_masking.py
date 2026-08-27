"""A notification must not name someone the thread it points at would mask.

The comment list resolves authors through ``resolve_visible_identities`` and the
template renders ``display_name`` when ``is_masked`` is set. The reply and reaction
notifications built their title and message from ``actor.username`` directly, so the
same person was "Member 2" in the thread and their real username in the notification.

That matters more here than on a page: a ``NotificationLog`` insert is picked up by
``enqueue_native_push`` and delivered to registered devices, and
``notification_text_alerts`` builds an SMS body from ``notification.title``. The name
leaves the app - onto a lock screen, into a text message.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.notifications.comment_notifications import notify_reaction, notify_reply


def _profile(visibility: str = VisibilityChoice.ANYONE) -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(profile_visibility=visibility)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


class CommentNotificationMaskingTests(TestCase):
    """The actor is named only when the recipient may see who they are."""

    def setUp(self):
        super().setUp()
        self.recipient = _profile()
        self.hidden_actor = _profile(VisibilityChoice.NO_ONE)
        self.open_actor = _profile()

        location = Location.objects.create(latitude=46.2, longitude=-69.8)
        self.pin = Pin.objects.create(profile=self.recipient, location=location, name="Quarry")
        self.comment = Comment.objects.create(pin=self.pin, profile=self.recipient, text="anyone been recently?")

    def _latest(self) -> NotificationLog:
        notification = NotificationLog.objects.filter(profile=self.recipient).order_by("-pk").first()
        self.assertIsNotNone(notification, "no notification was created")
        return notification

    def test_the_thread_masks_this_actor(self):
        # Premise: the notification is inconsistent with the page it links to.
        self.assertFalse(self.hidden_actor.can_view_profile(self.recipient))
        self.assertTrue(self.open_actor.can_view_profile(self.recipient))

    def test_a_hidden_repliers_username_is_not_in_the_notification(self):
        reply = Comment.objects.create(pin=self.pin, profile=self.hidden_actor, text="last week", parent=self.comment)
        notify_reply(self.hidden_actor, self.comment, reply)

        notification = self._latest()
        self.assertNotIn(self.hidden_actor.username, notification.title)
        self.assertNotIn(self.hidden_actor.username, notification.message)

    def test_a_visible_repliers_username_still_is(self):
        reply = Comment.objects.create(pin=self.pin, profile=self.open_actor, text="last week", parent=self.comment)
        notify_reply(self.open_actor, self.comment, reply)

        self.assertIn(self.open_actor.username, self._latest().title)

    def test_a_hidden_reactors_username_is_not_in_the_notification(self):
        notify_reaction(self.hidden_actor, self.comment)

        notification = self._latest()
        self.assertNotIn(self.hidden_actor.username, notification.title)
        self.assertNotIn(self.hidden_actor.username, notification.message)

    def test_a_visible_reactors_username_still_is(self):
        notify_reaction(self.open_actor, self.comment)

        self.assertIn(self.open_actor.username, self._latest().title)

    def test_the_notification_is_still_sent(self):
        # Masking the name must not suppress the event - the recipient is entitled
        # to know someone replied, just not necessarily who.
        notify_reaction(self.hidden_actor, self.comment)

        self.assertEqual(NotificationLog.objects.filter(profile=self.recipient).count(), 1)
        self.assertIn("reacted", self._latest().title)

    def test_the_deep_link_is_unchanged(self):
        notify_reaction(self.hidden_actor, self.comment)

        self.assertIn(f"#comment-{self.comment.pk}", self._latest().url)
