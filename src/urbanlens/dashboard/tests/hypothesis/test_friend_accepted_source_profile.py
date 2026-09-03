"""Every "friend request accepted" notification must name who accepted.

Three separate code paths raise `FRIEND_ACCEPTED`, and one of them omitted
`source_profile`. The external API's `NotificationSerializer` exposes that field,
so a client rendering the notification had no actor to link back to - while the
message text and the url in the very same row both referred to that profile.

Two paths raise it today, and why both exist:

- `services.social.friendship.request_or_accept_friendship` - the combined
  "befriend" entry point, which accepts an existing inbound request rather than
  creating a second one.
- `services.social.friendship.accept_friend_request` - the explicit accept.
  **This was the path missing it**, ported verbatim from the old controller
  during an extraction that was kept behaviour-preserving.

There was a third: `controllers.friendship.FriendController.friend_request_respond`
built its own notification until 2026-08-29 (`1899a8e64`), and now calls
`accept_friend_request`. The HTMX path still raises the notification; it just no
longer has its own copy of the code that can be wrong.

The completeness test at the bottom is the point: a new path would otherwise
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

    #: How a notification is raised. ``notify`` is the sanctioned entry point
    #: (see NotificationQuerySet.notify - it applies the recipient's mute
    #: preferences); ``create`` still works and is what it calls. Matching only
    #: ``create`` is why this scan silently found nothing: every site here moved
    #: to ``notify`` and the walk kept reporting an empty list of offenders.
    _RAISE_METHODS = ("notify", "create")

    def _accepted_sites(self) -> list[tuple[str, bool]]:
        """Every call raising FRIEND_ACCEPTED, and whether it names the actor.

        Returns:
            ``(location, sets_source_profile)`` per site, in file order.
        """
        sites = []
        for path in self._MODULES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") in self._RAISE_METHODS):
                    continue
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                if "notification_type" not in kwargs:
                    continue
                raises_accepted = any(
                    kw.arg == "notification_type" and getattr(kw.value, "attr", "") == "FRIEND_ACCEPTED" for kw in node.keywords
                )
                if raises_accepted:
                    sites.append((f"{path.name}:{node.lineno}", "source_profile" in kwargs))
        return sites

    def test_no_site_omits_it(self) -> None:
        self.assertEqual([location for location, names_actor in self._accepted_sites() if not names_actor], [])

    def test_the_scan_still_finds_the_sites(self) -> None:
        """Guard against the check above passing because it matched nothing.

        Counts what the AST walk actually found rather than a separate string
        search. The two disagreed, and both were wrong in different ways:

        - The string count expected 3 and read 2. The third site was
          ``FriendController.friend_request_respond``, which stopped building its
          own notification on 2026-08-29 (``1899a8e64``) and now calls
          ``accept_friend_request``. One fewer place to get wrong, not a lost
          notification - the HTMX path still raises it, through the service.
        - The AST walk read **0**, because it matched ``.create(`` and every site
          had moved to ``.notify(``. ``test_no_site_omits_it`` was therefore
          asserting that an empty list is empty, and had been since the move.

        Deriving the guard from the walk itself is what makes them agree: a scan
        that stops matching now fails here instead of passing silently there.
        """
        self.assertGreaterEqual(len(self._accepted_sites()), 2, "expected at least two FRIEND_ACCEPTED sites - have they moved out of these modules, or changed how they raise?")
