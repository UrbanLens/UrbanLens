"""Group chat routes that delete a message or share a pin: only the right member may do either."""

from __future__ import annotations

from datetime import timedelta
import os

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model

from urbanlens.core.tests.oauth import first_party_application
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupMessage, GroupMessageShare
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.group_chats import (
    create_group_chat,
    create_group_message,
    share_pin_in_group_message,
)

AccessToken = get_access_token_model()


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
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
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # absorbs the bootstrap site-admin promotion
        self.sender = _profile()
        self.member = _profile()
        self.outsider = _profile()
        _befriend(self.sender, self.member)
        self.group = create_group_chat(self.sender, "Explorers", [self.member])

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class GroupMessageDeleteRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.message = create_group_message(self.sender, self.group, "meet at the gate")
        self.url = reverse("messages.group.delete", args=[self.group.uuid, self.message.pk])

    def _deleted(self) -> bool:
        self.message.refresh_from_db()
        return self.message.deleted_at is not None

    def test_sender_deletes_their_message(self) -> None:
        self.client.force_login(self.sender.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._deleted())

    def test_another_member_is_refused_and_the_message_survives(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self._deleted())

    def test_non_member_gets_404(self) -> None:
        self.client.force_login(self.outsider.user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._deleted())

    def test_a_message_from_another_group_is_404_through_this_one(self) -> None:
        other_group = create_group_chat(self.sender, "Elsewhere", [self.member])
        other_message = create_group_message(self.sender, other_group, "elsewhere")
        self.client.force_login(self.sender.user)

        response = self.client.post(reverse("messages.group.delete", args=[self.group.uuid, other_message.pk]))

        self.assertEqual(response.status_code, 404)
        other_message.refresh_from_db()
        self.assertIsNone(other_message.deleted_at)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertFalse(self._deleted())

    def test_deleting_a_pin_share_message_revokes_the_pending_share(self) -> None:
        pin = baker.make(Pin, profile=self.sender)
        share_message = share_pin_in_group_message(self.sender, self.group, pin, "look")
        pin_share = PinShare.objects.get(pin=pin, to_profile=self.member)
        self.client.force_login(self.sender.user)

        self.client.post(reverse("messages.group.delete", args=[self.group.uuid, share_message.pk]))

        self.assertNotEqual(
            PinShare.objects.filter(pk=pin_share.pk).values_list("status", flat=True).first(), PinShareStatus.PENDING
        )


class GroupSharePinRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.sender, name="Old Mill")
        self.url = reverse("messages.group.share.pin", args=[self.group.uuid])

    def test_sender_shares_their_own_pin_to_the_connected_member(self) -> None:
        self.client.force_login(self.sender.user)

        response = self.client.post(self.url, {"pin_slug": self.pin.slug, "body": "look"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(PinShare.objects.filter(pin=self.pin).values_list("to_profile_id", flat=True)), [self.member.pk]
        )

    def test_a_member_cannot_share_somebody_elses_pin(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url, {"pin_slug": self.pin.slug})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PinShare.objects.filter(pin=self.pin).exists())
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())

    def test_non_member_gets_404_and_nothing_is_shared(self) -> None:
        own_pin = baker.make(Pin, profile=self.outsider)
        self.client.force_login(self.outsider.user)

        response = self.client.post(self.url, {"pin_slug": own_pin.slug})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PinShare.objects.exists())
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())

    def test_a_missing_pin_slug_is_a_4xx(self) -> None:
        self.client.force_login(self.sender.user)

        response = self.client.post(self.url, {})

        self.assertIn(response.status_code, range(400, 500))
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"pin_slug": self.pin.slug}))
        self.assertFalse(PinShare.objects.exists())


class GroupSharePinRespondRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.sender, name="Old Mill")
        self.message = share_pin_in_group_message(self.sender, self.group, self.pin, "look")
        self.share = PinShare.objects.get(pin=self.pin, to_profile=self.member)
        self.url = reverse("messages.group.share.pin.respond", args=[self.group.uuid, self.message.pk])

    def _status(self) -> str:
        self.share.refresh_from_db()
        return self.share.status

    def test_recipient_rejects_their_copy(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url, {"action": "reject"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._status(), PinShareStatus.REJECTED)

    def test_recipient_accepts_and_gets_a_pin_of_their_own(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url, {"action": "accept"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._status(), PinShareStatus.ACCEPTED)
        self.assertTrue(Pin.objects.filter(profile=self.member).exists())

    def test_the_sender_has_no_copy_to_answer(self) -> None:
        self.client.force_login(self.sender.user)

        response = self.client.post(self.url, {"action": "accept"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(), PinShareStatus.PENDING)

    def test_non_member_gets_404(self) -> None:
        self.client.force_login(self.outsider.user)

        response = self.client.post(self.url, {"action": "accept"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(), PinShareStatus.PENDING)

    def test_an_unknown_action_is_a_400_and_the_share_stays_pending(self) -> None:
        self.client.force_login(self.member.user)

        response = self.client.post(self.url, {"action": "steal"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._status(), PinShareStatus.PENDING)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"action": "accept"}))
        self.assertEqual(self._status(), PinShareStatus.PENDING)


class ExternalGroupPinShareRouteTests(_GroupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.sender, name="Old Mill")
        self.url = reverse("external_api:messages.groups.share.pin", args=[self.group.uuid])

    def _token(self, profile: Profile) -> dict:
        token = AccessToken.objects.create(
            user=profile.user,
            application=first_party_application(),
            token=f"tok-{os.urandom(8).hex()}",
            expires=timezone.now() + timedelta(hours=1),
            scope=f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}",
        )
        return {"HTTP_AUTHORIZATION": f"Bearer {token.token}"}

    def _post(self, profile: Profile | None, body):
        headers = self._token(profile) if profile is not None else {}
        return self.client.post(self.url, body, content_type="application/json", **headers)

    def test_sender_shares_their_pin_and_a_retry_with_the_same_client_uuid_does_not_reshare(self) -> None:
        body = {"shared_pin_id": self.pin.slug, "body": "look", "client_uuid": "0b8c6e1e-5d8f-4b8e-9a53-0d7e7b9b4f10"}

        first = self._post(self.sender, body)
        second = self._post(self.sender, body)

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(PinShare.objects.filter(pin=self.pin, to_profile=self.member).count(), 1)
        self.assertEqual(GroupMessageShare.objects.filter(message__group=self.group).count(), 1)

    def test_a_member_cannot_share_somebody_elses_pin(self) -> None:
        response = self._post(self.member, {"shared_pin_id": self.pin.slug, "body": "x"})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PinShare.objects.exists())

    def test_non_member_gets_404(self) -> None:
        response = self._post(
            self.outsider, {"shared_pin_id": baker.make(Pin, profile=self.outsider).slug, "body": "x"}
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())

    def test_anonymous_is_refused(self) -> None:
        response = self._post(None, {"shared_pin_id": self.pin.slug})

        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(PinShare.objects.exists())

    def test_a_missing_pin_id_is_a_400(self) -> None:
        response = self._post(self.sender, {"body": "no pin"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())

    def test_a_json_array_body_is_a_400(self) -> None:
        response = self._post(self.sender, [1, 2])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(GroupMessage.objects.filter(group=self.group).exists())
