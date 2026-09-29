"""Delete routes on a user's own pins, lists, albums and trips: the owner deletes, nobody else does."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership
from urbanlens.dashboard.models.visits.model import PinVisit


class _OwnerAndStranger(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile
        self.pin = baker.make(Pin, profile=self.owner, name="Old Mill")

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


class PinNoteDeleteRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.note = PinNote.objects.create(pin=self.pin, text="Catwalk is rotten")
        self.url = reverse("pin.note.delete", args=[self.pin.slug, self.note.pk])

    def test_owner_deletes_the_note(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinNote.objects.filter(pk=self.note.pk).exists())

    def test_stranger_gets_404_and_the_note_survives(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinNote.objects.filter(pk=self.note.pk).exists())

    def test_a_note_on_another_pin_is_not_reachable_through_the_owners_pin(self) -> None:
        other_pin = baker.make(Pin, profile=self.stranger)
        other_note = PinNote.objects.create(pin=other_pin, text="theirs")
        self.client.force_login(self.owner_user)

        response = self.client.delete(reverse("pin.note.delete", args=[self.pin.slug, other_note.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinNote.objects.filter(pk=other_note.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.delete(self.url))
        self.assertTrue(PinNote.objects.filter(pk=self.note.pk).exists())


class PinNoteCreateRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("pin.notes", args=[self.pin.slug])

    def test_owner_adds_a_note(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, {"text": "Bring a respirator"}, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(PinNote.objects.filter(pin=self.pin).values_list("text", flat=True)), ["Bring a respirator"]
        )

    def test_stranger_gets_404_and_no_note_is_added(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url, {"text": "graffiti"}, content_type="application/json")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(PinNote.objects.filter(pin=self.pin).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"text": "x"}, content_type="application/json"))
        self.assertFalse(PinNote.objects.filter(pin=self.pin).exists())

    def test_blank_text_is_a_400(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, {"text": "   "}, content_type="application/json")

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinNote.objects.filter(pin=self.pin).exists())

    @pytest.mark.xfail(
        strict=True,
        raises=AttributeError,
        reason="P29 bug: PinNotesView.post calls .get() on whatever JSON decodes, so a JSON array body raises (500)",
    )
    def test_a_json_array_body_is_a_4xx(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, data="[]", content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))

    @pytest.mark.xfail(
        strict=True,
        raises=AttributeError,
        reason="P29 bug: PinNotesView.post passes a non-string 'text' to create_pin_note, whose .strip() raises (500)",
    )
    def test_a_non_string_text_is_a_4xx(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, {"text": 5}, content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))


class PinVisitDeleteRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.visit = baker.make(PinVisit, pin=self.pin)
        self.url = reverse("pin.visit.delete", args=[self.pin.slug, self.visit.pk])

    def test_owner_deletes_the_visit(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinVisit.objects.filter(pk=self.visit.pk).exists())

    def test_stranger_gets_404_and_the_visit_survives(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinVisit.objects.filter(pk=self.visit.pk).exists())

    def test_a_visit_is_not_reachable_through_a_different_pin_of_the_same_owner(self) -> None:
        second_pin = baker.make(Pin, profile=self.owner)
        self.client.force_login(self.owner_user)

        response = self.client.post(reverse("pin.visit.delete", args=[second_pin.slug, self.visit.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinVisit.objects.filter(pk=self.visit.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(PinVisit.objects.filter(pk=self.visit.pk).exists())


class PinAlbumDeleteRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.album = baker.make(Album, parent_pin=self.pin, profile=self.owner, name="Exteriors")
        self.image = baker.make(Image, profile=self.owner, pin=self.pin)
        AlbumItem.objects.create(album=self.album, image=self.image)
        self.url = reverse("pin.albums.delete", args=[self.pin.slug, self.album.slug])

    def test_owner_deletes_the_album_and_keeps_its_photos(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Album.objects.filter(pk=self.album.pk).exists())
        self.assertTrue(Image.objects.filter(pk=self.image.pk).exists())

    def test_stranger_gets_404_and_the_album_survives(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Album.objects.filter(pk=self.album.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(Album.objects.filter(pk=self.album.pk).exists())


class PinAlbumRemovePhotosRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.album = baker.make(Album, parent_pin=self.pin, profile=self.owner, name="Exteriors")
        self.image = baker.make(Image, profile=self.owner, pin=self.pin)
        self.kept = baker.make(Image, profile=self.owner, pin=self.pin)
        AlbumItem.objects.create(album=self.album, image=self.image)
        AlbumItem.objects.create(album=self.album, image=self.kept)
        self.url = reverse("pin.albums.remove", args=[self.pin.slug, self.album.slug])

    def _members(self) -> set[int]:
        return set(AlbumItem.objects.filter(album=self.album).values_list("image_id", flat=True))

    def test_owner_removes_only_the_named_photo_and_the_photo_itself_survives(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, {"image_ids": [self.image.pk]}, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 1)
        self.assertEqual(self._members(), {self.kept.pk})
        self.assertTrue(Image.objects.filter(pk=self.image.pk).exists())

    def test_stranger_gets_404_and_membership_is_unchanged(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url, {"image_ids": [self.image.pk]}, content_type="application/json")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._members(), {self.image.pk, self.kept.pk})

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(
            self.client.post(self.url, {"image_ids": [self.image.pk]}, content_type="application/json")
        )
        self.assertEqual(self._members(), {self.image.pk, self.kept.pk})

    def test_non_list_image_ids_remove_nothing(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, {"image_ids": "all"}, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["removed"], 0)
        self.assertEqual(self._members(), {self.image.pk, self.kept.pk})

    @pytest.mark.xfail(
        strict=True,
        raises=AttributeError,
        reason="P29 bug: albums._parse_body returns whatever JSON decodes, so a JSON array body reaches .get() and raises (500)",
    )
    def test_a_json_array_body_is_a_4xx(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, data=f"[{self.image.pk}]", content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))


class PinListRemoveItemRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.pin_list = baker.make(PinList, profile=self.owner, name="Weekend")
        self.item = baker.make(PinListItem, pin_list=self.pin_list, pin=self.pin)
        self.url = reverse("lists.items.remove", args=[self.pin_list.slug, self.item.pk])

    def test_owner_removes_the_item_and_the_pin_survives(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(PinListItem.objects.filter(pk=self.item.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_stranger_gets_404_and_the_item_survives(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(PinListItem.objects.filter(pk=self.item.pk).exists())

    def test_an_item_of_another_list_is_not_removed_through_the_owners_list(self) -> None:
        their_list = baker.make(PinList, profile=self.stranger, name="Theirs")
        their_item = baker.make(PinListItem, pin_list=their_list, pin=baker.make(Pin, profile=self.stranger))
        self.client.force_login(self.owner_user)

        self.client.post(reverse("lists.items.remove", args=[self.pin_list.slug, their_item.pk]))

        self.assertTrue(PinListItem.objects.filter(pk=their_item.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(PinListItem.objects.filter(pk=self.item.pk).exists())


class TripActivityDeleteRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.trip = baker.make(Trip, creator=self.owner, name="Rust Belt", allow_edit_activities=Trip.PERM_ORGANIZERS)
        TripMembership.objects.create(trip=self.trip, profile=self.owner, status=TripMembership.STATUS_JOINED)
        self.member_user = baker.make(User)
        TripMembership.objects.create(
            trip=self.trip, profile=self.member_user.profile, status=TripMembership.STATUS_JOINED
        )
        self.activity = baker.make(TripActivity, trip=self.trip, pin=self.pin, location=self.pin.location)
        self.url = reverse("trips.activity.delete", args=[self.trip.slug, self.activity.pk])

    def test_creator_deletes_the_activity(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripActivity.objects.filter(pk=self.activity.pk).exists())

    def test_a_member_below_the_trips_edit_level_is_refused_and_the_activity_survives(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(TripActivity.objects.filter(pk=self.activity.pk).exists())

    def test_non_member_gets_404_and_the_activity_survives(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertTrue(TripActivity.objects.filter(pk=self.activity.pk).exists())

    def test_an_activity_of_another_trip_is_not_reachable_through_this_one(self) -> None:
        other_trip = baker.make(Trip, creator=self.stranger)
        other_activity = baker.make(TripActivity, trip=other_trip, pin=self.pin, location=self.pin.location)
        self.client.force_login(self.owner_user)

        response = self.client.delete(reverse("trips.activity.delete", args=[self.trip.slug, other_activity.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(TripActivity.objects.filter(pk=other_activity.pk).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.delete(self.url))
        self.assertTrue(TripActivity.objects.filter(pk=self.activity.pk).exists())
