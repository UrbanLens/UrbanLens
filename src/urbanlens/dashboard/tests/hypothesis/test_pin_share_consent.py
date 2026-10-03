"""A pin share reaches its recipient only as far as the sender consented, and is copied as of acceptance (P193).

What a share consents to (``docs/PRIVACY_MODEL.md`` §5): the place as it was when shared (``PinShare.location``),
the site's objective facts (type, indoor/outdoor, dates, security indicators), a name only when the sender typed one
(``shared_name``), the photos they ticked, the map they attached, the child pins they bundled, and their message.
Anything else - their own name for the pin, where it is now, their notes, ratings, styling, labels, aliases, links,
custom fields, visits, other photos, other children - never reaches the recipient, before or after acceptance,
including whatever the sender adds or changes between sharing and acceptance.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.choices import SecurityLevel
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.custom_fields.model import CustomField, CustomFieldEntity, CustomFieldValue
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupMessageShare
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.global_search.parser import parse_query
from urbanlens.dashboard.services.global_search.providers import PinSearchProvider
from urbanlens.dashboard.services.messaging.direct_message_shares import share_pin_in_message
from urbanlens.dashboard.services.messaging.group_chats import create_group_chat, share_pin_in_group_message
from urbanlens.dashboard.services.sharing.pin_sharing import apply_pin_share_response, create_pin_share

#: The sender's own name for their pin. Plain words, so an autoescaped page cannot hide it from assertNotContains.
SENDER_NAME = "zzq round the back way in"
OFFICIAL_NAME = "Consolidated Mill"


def _profile(username: str) -> Profile:
    profile = User.objects.create_user(username=username).profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


def _befriend(a: Profile, b: Profile) -> None:
    Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


class _ShareFixture(TestCase):
    """A sender, a friend to share with, and the sender's named pin at a place with an official name."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.sender = _profile("consentsender")
        self.recipient = _profile("consentrecipient")
        _befriend(self.sender, self.recipient)
        self.location = baker.make(
            Location,
            latitude=41.5,
            longitude=-73.9,
            official_name=OFFICIAL_NAME,
            street_number="12",
            route="Mill Road",
        )
        self.elsewhere = baker.make(
            Location, latitude=40.1, longitude=-75.2, official_name=None, street_number="99", route="Hidden Lane"
        )
        self.pin = baker.make(
            Pin,
            profile=self.sender,
            location=self.location,
            parent_pin=None,
            name=SENDER_NAME,
            name_is_user_provided=True,
        )

    def _share(self, **kwargs):
        return create_pin_share(self.sender, self.recipient, self.pin, **kwargs)

    def _accept(self, share) -> Pin:
        # Fetched fresh, as PinShareRespondView does: the instance _share returned still holds the pin it was given.
        share = PinShare.objects.select_related("pin", "notification").get(pk=share.pk)
        new_pin, _message = apply_pin_share_response(share, "accept")
        self.assertIsNotNone(new_pin)
        return new_pin

    def _move_pin_elsewhere(self) -> None:
        self.pin.location = self.elsewhere
        self.pin.save(update_fields=["location"])


class PendingShareShowsOnlyWhatWasConsentedTests(_ShareFixture):
    """Every surface a share reaches its recipient through, before (and after) they answer it."""

    def test_the_notification_names_the_place_not_the_senders_pin(self) -> None:
        share = self._share()

        message = NotificationLog.objects.get(pk=share.notification_id).message

        self.assertNotIn(SENDER_NAME, message)
        self.assertIn(OFFICIAL_NAME, message)

    def test_the_notification_uses_a_name_the_sender_chose_to_share(self) -> None:
        share = self._share(shared_name="The old mill")

        self.assertIn("The old mill", NotificationLog.objects.get(pk=share.notification_id).message)

    def test_the_share_page_names_the_place_not_the_senders_pin(self) -> None:
        share = self._share()
        self.client.force_login(self.recipient.user)

        response = self.client.get(reverse("pin.share.detail", args=[share.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SENDER_NAME)
        self.assertContains(response, OFFICIAL_NAME)

    def test_a_rename_after_sharing_does_not_reach_the_share_page(self) -> None:
        share = self._share(shared_name="The old mill")
        Pin.objects.filter(pk=self.pin.pk).update(name="zzq renamed to my own house")
        self.client.force_login(self.recipient.user)

        response = self.client.get(reverse("pin.share.detail", args=[share.pk]))

        self.assertNotContains(response, "zzq renamed")
        self.assertContains(response, "The old mill")

    def test_moving_the_pin_after_sharing_does_not_reveal_where_it_went(self) -> None:
        Pin.objects.filter(pk=self.pin.pk).update(name=None)
        self.pin.refresh_from_db()
        share = self._share()
        self._move_pin_elsewhere()
        self.client.force_login(self.recipient.user)

        response = self.client.get(reverse("pin.share.detail", args=[share.pk]))

        self.assertNotContains(response, "Hidden Lane")
        self.assertContains(response, "Mill Road")

    def test_bundled_children_are_named_by_their_places_not_the_senders_names(self) -> None:
        child_location = baker.make(Location, latitude=41.501, longitude=-73.901, official_name="Boiler House")
        child = baker.make(
            Pin, profile=self.sender, location=child_location, parent_pin=self.pin, name="zzq my stash spot"
        )
        share = self._share(children=[child])
        self.client.force_login(self.recipient.user)

        response = self.client.get(reverse("pin.share.detail", args=[share.pk]))

        self.assertNotContains(response, "zzq my stash spot")
        self.assertContains(response, "Boiler House")

    def test_the_received_list_names_the_place_not_the_senders_pin(self) -> None:
        self._share()
        self.client.force_login(self.recipient.user)

        response = self.client.get(reverse("memories.sharing.received"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, SENDER_NAME)
        self.assertContains(response, OFFICIAL_NAME)

    def test_a_direct_message_share_card_names_the_place_not_the_senders_pin(self) -> None:
        message = share_pin_in_message(self.sender, self.recipient, self.pin, "look at this")
        Pin.objects.filter(pk=self.pin.pk).update(name="zzq renamed to my own house")

        html = render_to_string(
            "dashboard/partials/messages/_message_share_card.html",
            {"share": message.share, "viewer_id": self.recipient.pk, "partner": self.sender},
        )

        self.assertNotIn(SENDER_NAME, html)
        self.assertNotIn("zzq renamed", html)
        self.assertIn(OFFICIAL_NAME, html)

    def test_a_group_share_names_the_place_and_shows_nothing_to_a_member_it_was_not_shared_with(self) -> None:
        stranger = _profile("consentgroupstranger")
        _befriend(self.recipient, stranger)
        group = create_group_chat(self.recipient, "Explorers", [self.sender, stranger])

        message = share_pin_in_group_message(self.sender, group, self.pin, "")

        self.assertNotIn(SENDER_NAME, message.body)
        recipient_share = GroupMessageShare.objects.get(message=message, recipient=self.recipient)
        self.assertFalse(GroupMessageShare.objects.filter(message=message, recipient=stranger).exists())
        recipient_card = render_to_string(
            "dashboard/partials/messages/_group_share_card.html",
            {"message": message, "viewer_share": recipient_share, "viewer_id": self.recipient.pk, "group": group},
        )
        stranger_card = render_to_string(
            "dashboard/partials/messages/_group_share_card.html",
            {"message": message, "viewer_share": None, "viewer_id": stranger.pk, "group": group},
        )
        self.assertNotIn(SENDER_NAME, recipient_card)
        self.assertIn(OFFICIAL_NAME, recipient_card)
        for leaked in (SENDER_NAME, OFFICIAL_NAME, "Mill Road"):
            self.assertNotIn(leaked, stranger_card)


class AcceptedCopyHoldsOnlyWhatWasConsentedTests(_ShareFixture):
    """What acceptance copies, read at acceptance, against what the share consented to."""

    def test_an_unnamed_share_is_named_from_the_place_and_not_flagged_as_user_named(self) -> None:
        new_pin = self._accept(self._share())

        self.assertEqual(new_pin.name, OFFICIAL_NAME)
        self.assertFalse(new_pin.name_is_user_provided)

    def test_a_name_the_sender_chose_to_share_is_kept_as_a_user_name(self) -> None:
        new_pin = self._accept(self._share(shared_name="The old mill"))

        self.assertEqual(new_pin.name, "The old mill")
        self.assertTrue(new_pin.name_is_user_provided)

    def test_the_sites_facts_are_read_as_of_acceptance(self) -> None:
        """Jess, 2026-10-02: copied as of acceptance, to the degree consented. The facts are consented."""
        share = self._share()
        Pin.objects.filter(pk=self.pin.pk).update(
            cameras=SecurityLevel.EVERYWHERE,
            fences=SecurityLevel.SOME,
            pin_type="building",
            pin_type_is_user_provided=True,
            indoor_outdoor="indoor",
            date_abandoned=datetime.date(1994, 6, 15),
        )

        new_pin = self._accept(share)

        self.assertEqual((new_pin.cameras, new_pin.fences), (SecurityLevel.EVERYWHERE, SecurityLevel.SOME))
        self.assertEqual((new_pin.pin_type, new_pin.pin_type_is_user_provided), ("building", True))
        self.assertEqual(new_pin.indoor_outdoor, "indoor")
        self.assertEqual(new_pin.date_abandoned, datetime.date(1994, 6, 15))

    def test_what_the_sender_adds_or_changes_after_sharing_in_unshared_categories_does_not_travel(self) -> None:
        share = self._share()
        Pin.objects.filter(pk=self.pin.pk).update(
            name="zzq renamed to my own house",
            description="zzq my notes on the loose panel",
            vulnerability=4,
            danger=5,
            priority=3,
            icon="zzq-icon",
            color="#abcdef",
        )
        self.pin.refresh_from_db()
        self.pin.labels.add(baker.make(Label, profile=self.sender, name="zzq private label"))
        PinNote.objects.create(pin=self.pin, text="zzq a note")
        PinAlias.objects.create(pin=self.pin, name="zzq an alias")
        PinLink.objects.create(pin=self.pin, name="zzq a link", url="https://example.com/zzq")
        field = CustomField.objects.create(profile=self.sender, entity_type=CustomFieldEntity.PIN, name="Gate code")
        CustomFieldValue.objects.create(field=field, pin=self.pin, value_text="zzq 4321")
        PinVisit.objects.create(pin=self.pin, visited_at=timezone.now(), notes="zzq went in")
        self._move_pin_elsewhere()

        new_pin = self._accept(share)

        self.assertEqual(new_pin.location_id, self.location.pk)
        self.assertEqual(new_pin.name, OFFICIAL_NAME)
        self.assertFalse(new_pin.description)
        self.assertEqual((new_pin.vulnerability, new_pin.danger, new_pin.priority), (0, 0, 0))
        self.assertNotEqual(new_pin.icon, "zzq-icon")
        self.assertNotEqual(new_pin.color, "#abcdef")
        self.assertFalse(new_pin.labels.exists())
        self.assertFalse(PinNote.objects.filter(pin=new_pin).exists())
        self.assertFalse(PinAlias.objects.filter(pin=new_pin, name__startswith="zzq").exists())
        self.assertFalse(PinLink.objects.filter(pin=new_pin).exists())
        self.assertFalse(CustomFieldValue.objects.filter(pin=new_pin).exists())
        self.assertFalse(PinVisit.objects.filter(pin=new_pin).exists())
        self.assertIsNone(new_pin.last_visited)

    def test_only_the_ticked_photos_travel_and_a_cover_set_later_to_another_does_not(self) -> None:
        ticked = Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, image="p/ticked.jpg")
        Image.objects.create(pin=self.pin, location=self.location, profile=self.sender, image="p/unticked.jpg")
        share = self._share(image_ids=[ticked.pk])
        added_later = Image.objects.create(
            pin=self.pin, location=self.location, profile=self.sender, image="p/later.jpg"
        )
        Pin.objects.filter(pk=self.pin.pk).update(cover_photo=added_later)

        new_pin = self._accept(share)

        self.assertEqual([image.image.name for image in Image.objects.filter(pin=new_pin)], ["p/ticked.jpg"])
        self.assertIsNone(new_pin.cover_photo_id)

    def test_a_child_added_after_sharing_does_not_travel(self) -> None:
        bundled = baker.make(Pin, profile=self.sender, location=baker.make(Location), parent_pin=self.pin)
        share = self._share(children=[bundled])
        baker.make(Pin, profile=self.sender, location=baker.make(Location), parent_pin=self.pin, name="zzq added later")

        new_pin = self._accept(share)

        self.assertEqual(Pin.objects.filter(profile=self.recipient).count(), 2)
        self.assertEqual(Pin.objects.get(profile=self.recipient, parent_pin=new_pin).location_id, bundled.location_id)

    def test_a_bundled_child_moved_under_an_unshared_pin_lands_under_the_root(self) -> None:
        first = baker.make(Pin, profile=self.sender, location=baker.make(Location), parent_pin=self.pin)
        second = baker.make(Pin, profile=self.sender, location=baker.make(Location), parent_pin=first)
        share = self._share(children=[first, second])
        unshared = baker.make(Pin, profile=self.sender, location=baker.make(Location), parent_pin=self.pin)
        Pin.objects.filter(pk=second.pk).update(parent_pin=unshared)

        new_pin = self._accept(share)

        received = Pin.objects.filter(profile=self.recipient)
        self.assertEqual(received.count(), 3)
        self.assertFalse(received.filter(location_id=unshared.location_id).exists())
        self.assertEqual(received.get(location_id=second.location_id).parent_pin_id, new_pin.pk)


class SharedWithMeSearchTests(_ShareFixture):
    """ "pins from <sender>" finds the pins their shares produced, by the place shared."""

    def test_a_pin_moved_after_sharing_does_not_tag_the_recipients_pin_at_its_new_place(self) -> None:
        received = self._accept(self._share())
        own = baker.make(Pin, profile=self.recipient, location=self.elsewhere, parent_pin=None, name="Recipient own")
        self._move_pin_elsewhere()

        results = PinSearchProvider().search(self.recipient, parse_query("pins from consentsender"), 20)

        self.assertEqual({result.object_slug for result in results}, {received.slug})
        self.assertNotIn(own.slug, {result.object_slug for result in results})
