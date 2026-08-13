"""Every "friend request accepted" notification must name who accepted.

Three separate code paths raise `FRIEND_ACCEPTED`, and one of them omitted
`source_profile`. The external API's `NotificationSerializer` exposes that field,
so a client rendering the notification had no actor to link back to - while the
message text and the url in the very same row both referred to that profile.

The three paths, and why they all exist:

- `services.social.friendship.request_or_accept_friendship` - the combined
  "befriend" entry point, which accepts an existing inbound request rather than
  creating a second one.
- `services.social.friendship.accept_friend_request` - the explicit accept.
  **This was the path missing it**, ported verbatim from the old controller
  during an extraction that was kept behaviour-preserving.
- `controllers.friendship.FriendController.friend_request_respond` - the HTMX
  view.

The completeness test at the bottom is the point: a fourth path would otherwise
reintroduce the same gap silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta.type import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.social import friendship as friendship_service


def _profile() -> Profile:
    return Profile.objects.get(user=baker.make("auth.User"))


class FriendAcceptedSourceProfileTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.requester = _profile()
        self.accepter = _profile()

    def _accepted_notification(self) -> NotificationLog:
        return NotificationLog.objects.get(notification_type=NotificationType.FRIEND_ACCEPTED)

    def test_accept_friend_request_names_the_accepter(self) -> None:
        """The path that was missing it."""
        Friendship.objects.create(from_profile=self.requester, to_profile=self.accepter, status=FriendshipStatus.REQUESTED)

        friendship_service.accept_friend_request(self.accepter, self.requester)

        self.assertEqual(self._accepted_notification().source_profile_id, self.accepter.pk)

    def test_request_or_accept_names_the_accepter(self) -> None:
        """The path that already set it - pinned so the two cannot drift apart."""
        Friendship.objects.create(from_profile=self.requester, to_profile=self.accepter, status=FriendshipStatus.REQUESTED)

        friendship_service.request_or_accept_friendship(self.accepter, self.requester)

        notification = self._accepted_notification()
        self.assertEqual(notification.profile_id, self.requester.pk, "the requester is the recipient")
        self.assertEqual(notification.source_profile_id, self.accepter.pk)

    def test_the_actor_named_matches_the_message_and_url(self) -> None:
        """source_profile must agree with the row's own text, not just be non-null."""
        Friendship.objects.create(from_profile=self.requester, to_profile=self.accepter, status=FriendshipStatus.REQUESTED)

        friendship_service.request_or_accept_friendship(self.accepter, self.requester)

        notification = self._accepted_notification()
        self.assertIn(self.accepter.username, notification.message)
        self.assertIn(str(self.accepter.slug or self.accepter.uuid), notification.url)


class EveryFriendAcceptedSiteSetsSourceProfileTests(SimpleTestCase):
    """Static completeness check across every module that raises this type."""

    _MODULES = (
        Path("src/urbanlens/dashboard/services/social/friendship.py"),
        Path("src/urbanlens/dashboard/controllers/friendship.py"),
    )

    def _sites_missing_source_profile(self) -> list[str]:
        missing = []
        for path in self._MODULES:
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "create"):
                    continue
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                if "notification_type" not in kwargs:
                    continue
                raises_accepted = any(
                    kw.arg == "notification_type" and getattr(kw.value, "attr", "") == "FRIEND_ACCEPTED" for kw in node.keywords
                )
                if raises_accepted and "source_profile" not in kwargs:
                    missing.append(f"{path.name}:{node.lineno}")
        return missing

    def test_no_site_omits_it(self) -> None:
        self.assertEqual(self._sites_missing_source_profile(), [])

    def test_the_scan_still_finds_the_sites(self) -> None:
        """Guard against the check passing because it matched nothing."""
        found = sum(path.read_text().count("NotificationType.FRIEND_ACCEPTED") for path in self._MODULES)

        self.assertGreaterEqual(found, 3, "expected three FRIEND_ACCEPTED sites - has this moved?")
