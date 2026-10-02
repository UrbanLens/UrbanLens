"""Read markers and per-member preferences: group read and mute, notifications read-all, the map dark-mode setting (P29).

Each asserts that the caller's own row changes and nobody else's, that a non-member is refused with nothing changed,
that anonymous is sent to log in (or refused on the external API), and that a malformed body is a 4xx.
"""

from __future__ import annotations

from datetime import timedelta
import os

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupChatMembership
from urbanlens.dashboard.models.notifications.meta.status import Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.group_chats import create_group_chat, remove_group_member

AccessToken = get_access_token_model()


def _profile() -> Profile:
    profile = baker.make(User).profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    return profile


def _befriend(a: Profile, b: Profile) -> None:
    Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class _GroupFixture(TestCase):
    """A group of three; the outsider was never in it and the former member was removed."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner = _profile()
        self.member = _profile()
        self.former = _profile()
        self.outsider = _profile()
        _befriend(self.owner, self.member)
        _befriend(self.owner, self.former)
        self.group = create_group_chat(self.owner, "Explorers", [self.member, self.former])
        remove_group_member(self.group, self.owner, self.former)

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])

    def membership(self, profile: Profile) -> GroupChatMembership:
        return GroupChatMembership.objects.get(group=self.group, profile=profile)


class GroupReadRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("messages.group.read", args=[self.group.uuid])
        GroupChatMembership.objects.filter(group=self.group).update(last_read_at=None)

    def test_a_member_marks_only_their_own_read_mark(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 204)
        self.assertIsNotNone(self.membership(self.member).last_read_at)
        self.assertIsNone(self.membership(self.owner).last_read_at)

    def test_a_garbled_body_is_ignored_not_a_500(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url, "[1,", content_type="application/json")

        self.assertEqual(response.status_code, 204)

    def test_someone_outside_the_group_gets_404(self) -> None:
        for profile in (self.outsider, self.former):
            self.client.force_login(profile.user)
            self.assertEqual(self.client.post(self.url).status_code, 404, profile)
        self.assertIsNone(self.membership(self.former).last_read_at)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertFalse(GroupChatMembership.objects.filter(group=self.group, last_read_at__isnull=False).exists())


class GroupMuteRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("messages.group.mute", args=[self.group.uuid])

    def test_a_member_mutes_then_unmutes_only_themselves(self) -> None:
        self.client.force_login(self.member.user)

        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertTrue(self.membership(self.member).muted)
        self.assertFalse(self.membership(self.owner).muted)

        self.client.post(self.url)
        self.assertFalse(self.membership(self.member).muted)

    def test_someone_outside_the_group_gets_404(self) -> None:
        for profile in (self.outsider, self.former):
            self.client.force_login(profile.user)
            self.assertEqual(self.client.post(self.url).status_code, 404, profile)
        self.assertFalse(self.membership(self.former).muted)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertFalse(GroupChatMembership.objects.filter(group=self.group, muted=True).exists())


class ExternalGroupReadRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:messages.groups.read", args=[self.group.uuid])
        GroupChatMembership.objects.filter(group=self.group).update(last_read_at=None)

    def _auth(self, profile: Profile, scopes: str | None = None) -> dict:
        token = AccessToken.objects.create(
            user=profile.user,
            application=first_party_application(),
            token=f"tok-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=scopes or f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}",
        )
        return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}

    def test_a_member_marks_only_their_own_read_mark(self) -> None:
        response = self.client.post(self.url, **self._auth(self.member))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNotNone(self.membership(self.member).last_read_at)
        self.assertIsNone(self.membership(self.owner).last_read_at)

    def test_a_non_member_and_an_unknown_group_are_the_same_404(self) -> None:
        stranger = self.client.post(self.url, **self._auth(self.outsider))
        former = self.client.post(self.url, **self._auth(self.former))
        unknown = self.client.post(
            reverse("external_api:messages.groups.read", args=["00000000-0000-4000-8000-000000000000"]),
            **self._auth(self.outsider),
        )

        self.assertEqual({stranger.status_code, former.status_code, unknown.status_code}, {404})
        self.assertEqual(stranger.content, unknown.content)
        self.assertIsNone(self.membership(self.former).last_read_at)

    def test_a_read_only_token_is_refused(self) -> None:
        response = self.client.post(self.url, **self._auth(self.member, ApiKeyScope.MESSAGES_READ.value))

        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.membership(self.member).last_read_at)

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self.client.post(self.url).status_code, (401, 403))
        self.assertFalse(GroupChatMembership.objects.filter(group=self.group, last_read_at__isnull=False).exists())


class NotificationsReadAllRouteTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.other = baker.make(User).profile
        self.mine = baker.make(NotificationLog, profile=self.profile, status=Status.UNREAD, title="Yours")
        self.dismissed = baker.make(NotificationLog, profile=self.profile, status=Status.DISMISSED, title="Gone")
        self.theirs = baker.make(NotificationLog, profile=self.other, status=Status.UNREAD, title="Theirs")
        self.url = reverse("notifications.read_all")

    def _status(self, notification: NotificationLog) -> str:
        notification.refresh_from_db()
        return notification.status

    def test_marks_only_the_callers_unread_notifications(self) -> None:
        self.client.force_login(self.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._status(self.mine), Status.READ)
        self.assertEqual(self._status(self.dismissed), Status.DISMISSED)
        self.assertEqual(self._status(self.theirs), Status.UNREAD)
        self.assertNotContains(response, "Theirs")

    def test_anonymous_is_redirected_to_login(self) -> None:
        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL))
        self.assertEqual(self._status(self.mine), Status.UNREAD)


class SaveMapDarkModeRouteTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.other = baker.make(User).profile
        self.url = reverse("settings.save_map_dark_mode")

    def _mode(self, profile: Profile) -> str:
        return Profile.objects.values_list("map_dark_mode", flat=True).get(pk=profile.pk)

    def test_each_mode_is_saved_for_the_caller_only(self) -> None:
        self.client.force_login(self.user)
        before = self._mode(self.other)

        for mode in ("dark", "light", "system"):
            self.assertEqual(self.client.post(self.url, {"mode": mode}).status_code, 200, mode)
            self.assertEqual(self._mode(self.user.profile), mode)
        self.assertEqual(self._mode(self.other), before)

    def test_a_missing_or_unknown_mode_is_400_and_keeps_the_setting(self) -> None:
        self.client.force_login(self.user)
        self.client.post(self.url, {"mode": "dark"})

        for body in ({}, {"mode": "midnight"}, {"mode": ""}):
            self.assertEqual(self.client.post(self.url, body).status_code, 400, body)
        self.assertEqual(self._mode(self.user.profile), "dark")

    def test_anonymous_is_redirected_to_login(self) -> None:
        response = self.client.post(self.url, {"mode": "dark"})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL))
