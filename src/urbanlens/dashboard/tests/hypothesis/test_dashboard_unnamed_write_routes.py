"""Dashboard write routes on a user's own check-ins, lists, floorplans and trip activities (P29).

Each asserts the owner's (or authorised member's) write lands, that anyone else is refused and nothing changes,
that anonymous is sent to log in (or, on the token-gated contact route, refused without a valid token), and that a
malformed request is a 4xx rather than a 500 or a silent wrong write.
"""

from __future__ import annotations

import datetime
import json

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.floorplans.model import Floorplan
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinMessage,
    SafetyCheckinStatus,
)
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership


class _OwnerAndStranger(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.owner_user = baker.make(User)
        self.owner = self.owner_user.profile
        self.stranger_user = baker.make(User)
        self.stranger = self.stranger_user.profile

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])


def _checkin(profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Hike",
        "checkin_by": timezone.now() + datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
    }
    return baker.make(SafetyCheckin, **{**defaults, **kwargs})


class SafetyCheckinCheckInRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.checkin = _checkin(self.owner, status=SafetyCheckinStatus.AWAITING_CHECKIN)
        self.url = reverse("safety.checkin.checkin", args=[self.checkin.slug])

    def _status(self) -> str:
        return SafetyCheckin.objects.values_list("status", flat=True).get(pk=self.checkin.pk)

    def test_the_owner_checks_in(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._status(), SafetyCheckinStatus.CHECKED_IN)

    def test_a_check_in_on_a_concluded_check_in_changes_nothing(self) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(status=SafetyCheckinStatus.FOUND_SAFE)
        self.client.force_login(self.owner_user)

        self.client.post(self.url)

        self.assertEqual(self._status(), SafetyCheckinStatus.FOUND_SAFE)

    def test_a_stranger_gets_404_and_cannot_check_the_owner_in(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self._status(), SafetyCheckinStatus.AWAITING_CHECKIN)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertEqual(self._status(), SafetyCheckinStatus.AWAITING_CHECKIN)


class SafetyCheckinLocationUpdateRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.checkin = _checkin(self.owner, live_location_sharing_enabled=True)
        self.url = reverse("safety.checkin.location.update", args=[self.checkin.slug])
        cache.delete(f"urbanlens:safety:location-update-throttle:{self.checkin.pk}")

    def _position(self) -> tuple:
        return SafetyCheckin.objects.values_list("live_latitude", "live_longitude", "live_location_accuracy").get(
            pk=self.checkin.pk
        )

    def test_the_owner_records_their_position(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url, {"latitude": "42.5", "longitude": "-73.25", "accuracy": "12"})

        self.assertEqual(response.status_code, 204)
        latitude, longitude, accuracy = self._position()
        self.assertEqual((float(latitude), float(longitude), accuracy), (42.5, -73.25, 12.0))

    def test_a_stranger_gets_404_and_records_nothing(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url, {"latitude": "1", "longitude": "1"}).status_code, 404)
        self.assertEqual(self._position(), (None, None, None))

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url, {"latitude": "1", "longitude": "1"}))
        self.assertEqual(self._position(), (None, None, None))

    def test_a_position_with_sharing_off_is_400_and_records_nothing(self) -> None:
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(live_location_sharing_enabled=False)
        self.client.force_login(self.owner_user)

        self.assertEqual(self.client.post(self.url, {"latitude": "1", "longitude": "1"}).status_code, 400)
        self.assertEqual(self._position(), (None, None, None))

    def test_a_point_off_the_globe_or_not_a_number_is_400_and_records_nothing(self) -> None:
        self.client.force_login(self.owner_user)
        bad = [
            {"latitude": "91", "longitude": "0"},
            {"latitude": "0", "longitude": "181"},
            {"latitude": "nan", "longitude": "0"},
            {"latitude": "0", "longitude": "inf"},
            {"latitude": "1e300", "longitude": "0"},
            {"latitude": "0", "longitude": "0", "accuracy": "nan"},
            {"latitude": "0", "longitude": "0", "accuracy": "-5"},
            {"latitude": "north", "longitude": "0"},
            {"longitude": "0"},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.client.post(self.url, body).status_code, 400)
        self.assertEqual(self._position(), (None, None, None))


class SafetyContactMessageRouteTests(_OwnerAndStranger):
    """The contact route's credential is the magic-link token in the URL, not a login."""

    def setUp(self) -> None:
        super().setUp()
        self.checkin = _checkin(self.owner)
        self.contact = baker.make(
            SafetyCheckinContact, checkin=self.checkin, email="mo@example.com", contact_profile=None
        )
        self.url = reverse("safety.contact.messages", args=[self.contact.token])

    def _messages(self) -> list[tuple]:
        return list(
            SafetyCheckinMessage.objects.filter(checkin=self.checkin).values_list(
                "body", "sender_profile", "sender_contact"
            )
        )

    def test_the_contact_posts_without_logging_in(self) -> None:
        response = self.client.post(self.url, {"body": "Are you out yet?"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._messages(), [("Are you out yet?", None, self.contact.pk)])

    def test_a_logged_in_stranger_holding_the_link_posts_as_the_contact_not_as_themselves(self) -> None:
        self.client.force_login(self.stranger_user)

        self.client.post(self.url, {"body": "hello"})

        self.assertEqual(self._messages(), [("hello", None, self.contact.pk)])

    def test_an_unknown_token_is_404_and_posts_nothing(self) -> None:
        url = reverse("safety.contact.messages", args=["00000000-0000-4000-8000-000000000000"])

        self.assertEqual(self.client.post(url, {"body": "hi"}).status_code, 404)
        self.assertEqual(self._messages(), [])

    def test_an_overlong_message_is_400_and_posts_nothing(self) -> None:
        self.assertEqual(self.client.post(self.url, {"body": "x" * 4001}).status_code, 400)
        self.assertEqual(self._messages(), [])

    def test_a_blank_or_json_body_posts_nothing_and_is_not_a_500(self) -> None:
        for response in (
            self.client.post(self.url, {"body": "   "}),
            self.client.post(self.url, data="[]", content_type="application/json"),
            self.client.post(self.url, data='{"body": "hi"}', content_type="application/json"),
        ):
            self.assertLess(response.status_code, 500)
        self.assertEqual(self._messages(), [])

    def test_a_message_to_an_archived_check_in_is_409(self) -> None:
        baker.make("dashboard.SafetyCheckinArchive", checkin=self.checkin)

        self.assertEqual(self.client.post(self.url, {"body": "late"}).status_code, 409)
        self.assertEqual(self._messages(), [])


class PinListDeleteRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.owner, name="Mill")
        self.pin_list = baker.make(PinList, profile=self.owner, name="Weekend")
        PinListItem.objects.create(pin_list=self.pin_list, pin=self.pin)
        self.url = reverse("lists.delete", args=[self.pin_list.slug])

    def test_the_owner_deletes_the_list_and_keeps_its_pins(self) -> None:
        self.client.force_login(self.owner_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(PinList.objects.filter(pk=self.pin_list.pk).exists())
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_a_stranger_gets_404_and_the_list_survives(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertTrue(PinListItem.objects.filter(pin_list=self.pin_list).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertTrue(PinList.objects.filter(pk=self.pin_list.pk).exists())

    def test_a_get_deletes_nothing(self) -> None:
        self.client.force_login(self.owner_user)

        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertTrue(PinList.objects.filter(pk=self.pin_list.pk).exists())


class FloorplanSaveRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.place = baker.make(Place, kind=PlaceKind.BUILDING, parent=baker.make(Place, kind=PlaceKind.PARCEL))
        location = baker.make(Location, latitude=41.733, longitude=-73.928, place=self.place)
        self.pin = baker.make(Pin, profile=self.owner, location=location, parent_pin=None, slug="hrsh-admin")
        self.url = reverse("pin.floorplan.save", args=[self.pin.slug])

    def _save(self, document: object):
        return self.client.post(self.url, data=json.dumps(document), content_type="application/json")

    def _plans(self, profile) -> list[str]:
        return list(Floorplan.objects.filter(profile=profile).values_list("name", flat=True))

    def test_the_owner_saves_a_plan(self) -> None:
        self.client.force_login(self.owner_user)

        response = self._save({"name": "As built", "floors": [{"level": 0, "name": "Ground"}]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._plans(self.owner), ["As built"])

    def test_a_save_naming_another_accounts_plan_leaves_it_alone(self) -> None:
        theirs = baker.make(Floorplan, place=self.place, profile=self.stranger, name="Theirs")
        self.client.force_login(self.owner_user)

        response = self._save({"uuid": str(theirs.uuid), "name": "Mine", "floors": []})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._plans(self.stranger), ["Theirs"])
        self.assertEqual(self._plans(self.owner), ["Mine"])

    def test_a_stranger_gets_404_and_saves_nothing(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self._save({"name": "Graffiti", "floors": []}).status_code, 404)
        self.assertFalse(Floorplan.objects.exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._save({"name": "x", "floors": []}))
        self.assertFalse(Floorplan.objects.exists())

    def test_a_malformed_document_is_4xx_and_saves_nothing(self) -> None:
        self.client.force_login(self.owner_user)
        for raw in ("[]", "null", '"text"', "{"):
            with self.subTest(body=raw):
                status = self.client.post(self.url, data=raw, content_type="application/json").status_code
                self.assertIn(status, range(400, 500))
        for document in (
            {"uuid": "not-a-uuid", "floors": []},
            {"uuid": 5, "floors": []},
            {"valid_from": 5, "floors": []},
            {"valid_from": "last spring", "floors": []},
        ):
            with self.subTest(document=document):
                self.assertIn(self._save(document).status_code, range(400, 500))
        self.assertFalse(Floorplan.objects.exists())

    def test_reading_a_version_that_is_not_a_uuid_finds_nothing_rather_than_failing(self) -> None:
        self.client.force_login(self.owner_user)
        self._save({"name": "As built", "floors": []})

        for name in ("pin.floorplan.json", "pin.floorplan.features"):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=[self.pin.slug]), {"version": "not-a-uuid"})
                self.assertEqual(response.status_code, 204)


class TripActivityStatusRouteTests(_OwnerAndStranger):
    def setUp(self) -> None:
        super().setUp()
        self.trip = baker.make(Trip, creator=self.owner, name="Rust Belt")
        TripMembership.objects.create(trip=self.trip, profile=self.owner, status=TripMembership.STATUS_JOINED)
        self.activity = baker.make(TripActivity, trip=self.trip, status=TripActivity.STATUS_PROPOSED, scheduled_at=None)
        self.url = reverse("trips.activity.status", args=[self.trip.slug, self.activity.pk])

    def _status(self, activity: TripActivity | None = None) -> str:
        return TripActivity.objects.values_list("status", flat=True).get(pk=(activity or self.activity).pk)

    def test_a_member_toggles_and_sets_the_status(self) -> None:
        self.client.force_login(self.owner_user)

        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertEqual(self._status(), TripActivity.STATUS_CONFIRMED)
        self.client.post(self.url, {"status": TripActivity.STATUS_CONFIRMED})
        self.assertEqual(self._status(), TripActivity.STATUS_CONFIRMED)
        self.client.post(self.url, {"status": TripActivity.STATUS_PROPOSED})
        self.assertEqual(self._status(), TripActivity.STATUS_PROPOSED)

    def test_an_activity_of_another_trip_is_404(self) -> None:
        other_trip = baker.make(Trip, creator=self.stranger, name="Elsewhere")
        foreign = baker.make(TripActivity, trip=other_trip, status=TripActivity.STATUS_PROPOSED)
        self.client.force_login(self.owner_user)

        response = self.client.post(reverse("trips.activity.status", args=[self.trip.slug, foreign.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(foreign), TripActivity.STATUS_PROPOSED)

    def test_a_non_member_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertEqual(self._status(), TripActivity.STATUS_PROPOSED)

    def test_an_invited_member_who_has_not_joined_is_refused(self) -> None:
        TripMembership.objects.create(trip=self.trip, profile=self.stranger, status=TripMembership.STATUS_INVITED)
        self.client.force_login(self.stranger_user)

        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assertEqual(self._status(), TripActivity.STATUS_PROPOSED)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertEqual(self._status(), TripActivity.STATUS_PROPOSED)

    def test_an_unrecognized_status_is_400_not_a_flip(self) -> None:
        """A typo for "proposed" sent to a proposed activity would otherwise confirm it - the opposite of the ask."""
        self.client.force_login(self.owner_user)

        for value in ("proposd", "completed", "CONFIRMED"):
            with self.subTest(status=value):
                self.assertEqual(self.client.post(self.url, {"status": value}).status_code, 400)
        self.assertEqual(self._status(), TripActivity.STATUS_PROPOSED)

    def test_a_malformed_body_is_4xx(self) -> None:
        self.client.force_login(self.owner_user)

        for raw in ("[]", '"text"', "{"):
            with self.subTest(body=raw):
                status = self.client.post(self.url, data=raw, content_type="application/json").status_code
                self.assertIn(status, range(400, 500))
        self.assertEqual(self._status(), TripActivity.STATUS_PROPOSED)
