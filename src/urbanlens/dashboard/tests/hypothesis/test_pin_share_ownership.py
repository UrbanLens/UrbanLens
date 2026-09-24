"""A pin share can only offer the sender's own pin, and only its own photos, map and child pins - enforced by the
service every share path goes through, not by each caller's lookup."""

from __future__ import annotations

from django.core import mail
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupMessage
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.messaging.direct_message_shares import share_pin_in_message
from urbanlens.dashboard.services.messaging.group_chats import create_group_chat, share_pin_in_group_message
from urbanlens.dashboard.services.sharing.pin_sharing import PinSharePermissionError, create_pin_share


def _profile() -> Profile:
    profile = baker.make("auth.User", email=f"{baker.random_gen.gen_string(10)}@example.com").profile
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


class _SharingTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        self.bystander = _profile()
        _befriend(self.sender, self.recipient)
        self.own_pin = baker.make_recipe("dashboard.pin", profile=self.sender)
        self.foreign_pin = baker.make_recipe("dashboard.pin", profile=self.bystander)


class CreatePinShareOwnershipTests(_SharingTestCase):
    def test_a_profile_cannot_share_someone_elses_pin(self) -> None:
        with pytest.raises(PinSharePermissionError):
            create_pin_share(self.sender, self.recipient, self.foreign_pin)

        self.assertFalse(PinShare.objects.filter(pin=self.foreign_pin).exists())

    def test_the_direct_message_wrapper_refuses_it_and_sends_nothing(self) -> None:
        with pytest.raises(PinSharePermissionError):
            share_pin_in_message(self.sender, self.recipient, self.foreign_pin, "look")

        self.assertFalse(DirectMessage.objects.filter(sender=self.sender).exists())
        self.assertFalse(PinShare.objects.filter(pin=self.foreign_pin).exists())

    def test_the_group_wrapper_refuses_it_before_posting_anything(self) -> None:
        group = create_group_chat(self.sender, "Crew", [self.recipient])

        with pytest.raises(PinSharePermissionError):
            share_pin_in_group_message(self.sender, group, self.foreign_pin, "look")

        self.assertFalse(GroupMessage.objects.filter(group=group, sender=self.sender).exists())
        self.assertFalse(PinShare.objects.filter(pin=self.foreign_pin).exists())

    def test_the_owner_can_still_share(self) -> None:
        share = create_pin_share(self.sender, self.recipient, self.own_pin)

        self.assertEqual(share.from_profile_id, self.sender.pk)


class CreatePinShareAttachmentTests(_SharingTestCase):
    def test_only_the_pins_own_photos_are_attached(self) -> None:
        own_photo = baker.make_recipe("dashboard.image", pin=self.own_pin, profile=self.sender)
        other_photo = baker.make_recipe("dashboard.image", pin=self.foreign_pin, profile=self.bystander)

        share = create_pin_share(self.sender, self.recipient, self.own_pin, image_ids=[own_photo.pk, other_photo.pk])

        self.assertEqual(set(share.images.values_list("pk", flat=True)), {own_photo.pk})

    def test_another_profiles_map_cannot_be_attached(self) -> None:
        foreign_map = baker.make(MarkupMap, profile=self.bystander)

        with pytest.raises(PinSharePermissionError):
            create_pin_share(self.sender, self.recipient, self.own_pin, markup_map=foreign_map)

        self.assertFalse(PinShare.objects.filter(pin=self.own_pin).exists())

    def test_only_descendants_of_the_shared_pin_are_bundled(self) -> None:
        child = baker.make(Pin, profile=self.sender, parent_pin=self.own_pin, location=baker.make("dashboard.Location"))
        unrelated = baker.make_recipe("dashboard.pin", profile=self.sender)

        share = create_pin_share(
            self.sender, self.recipient, self.own_pin, children=[child, unrelated, self.foreign_pin]
        )

        self.assertEqual(set(share.bundled_shares.values_list("pin_id", flat=True)), {child.pk})


class PinShareSendViewTests(_SharingTestCase):
    def test_an_email_preference_is_honoured_on_the_web_path_too(self) -> None:
        NotificationPreference.objects.update_or_create(
            profile=self.recipient, defaults={"pin_shared": DeliveryPreference.EMAIL}
        )
        self.client.force_login(self.sender.user)

        response = self.client.post(
            reverse("pin.share.send", kwargs={"pin_slug": self.own_pin.slug}), {"profile_id": self.recipient.pk}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(PinShare.objects.filter(pin=self.own_pin, to_profile=self.recipient).exists())
        self.assertEqual([message.to for message in mail.outbox], [[self.recipient.user.email]])
