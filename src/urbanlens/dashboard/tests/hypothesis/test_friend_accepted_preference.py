"""The friend_accepted delivery preference is consulted - it was the one dead stem.

Audit chunk 479 checked all 12 preference-covered notification types: eleven
consulted their toggle at creation; FRIEND_ACCEPTED was created
unconditionally at both of its sites, so a user who silenced it in settings
kept receiving it - a stored preference that did nothing, the same class the
"wire them all" decision fixed for text channels. Both sites now consult it,
without disturbing the acceptance flow's other effects (the request
notification still gets marked read, the friendship is still returned).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.services.social.friendship import accept_friend_request, request_or_accept_friendship


class FriendAcceptedPreferenceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.requester = baker.make(User).profile
        self.acceptor = baker.make(User).profile
        request_or_accept_friendship(self.requester, self.acceptor)

    def _accepted_notifications(self) -> int:
        return NotificationLog.objects.filter(
            profile=self.requester, notification_type=NotificationType.FRIEND_ACCEPTED
        ).count()

    def test_default_preference_still_notifies(self) -> None:
        friendship = accept_friend_request(self.acceptor, self.requester)
        self.assertIsNotNone(friendship)
        self.assertEqual(self._accepted_notifications(), 1)

    def test_none_preference_suppresses_the_notification_but_not_the_acceptance(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationPreference

        # The row is created lazily in production (the consultation's
        # AttributeError fallback covers its absence); create it here to set
        # the toggle.
        NotificationPreference.objects.create(profile=self.requester, friend_accepted=DeliveryPreference.NONE)

        friendship = accept_friend_request(self.acceptor, self.requester)

        self.assertIsNotNone(friendship, "silencing the notification must not break the acceptance itself")
        self.assertEqual(self._accepted_notifications(), 0)
