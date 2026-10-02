"""External API writes that cross an account boundary or a pin hierarchy: answering a shared pin, merging pins,
RSVPing to a trip (P29).

Each asserts the authorised caller's write lands, that anyone else is refused with a 404 and nothing changes, that
anonymous and a key without the write scope are refused, and that a malformed body is a 4xx.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.sharing.share_provenance import record_share_exposure
from urbanlens.dashboard.tests.hypothesis.external_api_helpers import ExternalApiRouteCase


class PinShareRespondRouteTests(ExternalApiRouteCase):
    """The owner fixture is the share's recipient; the stranger is a third account."""

    scopes = (ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE)
    read_scopes = (ApiKeyScope.PINS_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.sender_user = baker.make("auth.User")
        self.sender = self.sender_user.profile
        self.sender_pin = create_pin_for_profile(self.sender, name="Old Mill", latitude=42.5, longitude=-73.5).pin
        self.share = PinShare.objects.create(
            pin=self.sender_pin,
            location=self.sender_pin.location,
            from_profile=self.sender,
            to_profile=self.owner,
            status=PinShareStatus.PENDING,
        )
        record_share_exposure(self.share)
        self.url = reverse("external_api:pin-shares.respond", args=[self.share.pk])

    def _status(self) -> str:
        return PinShare.objects.values_list("status", flat=True).get(pk=self.share.pk)

    def _pins_from_share(self) -> int:
        return Pin.objects.filter(source_share=self.share).count()

    def test_the_recipient_accepts_and_gets_their_own_pin(self) -> None:
        response = self.send("post", self.url, {"action": "accept"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._status(), PinShareStatus.ACCEPTED)
        self.assertEqual(
            list(Pin.objects.filter(source_share=self.share).values_list("profile", flat=True)), [self.owner.pk]
        )

    def test_the_recipient_rejects_and_no_pin_is_made(self) -> None:
        self.assertEqual(self.send("post", self.url, {"action": "reject"}).status_code, 200)

        self.assertEqual(self._status(), PinShareStatus.REJECTED)
        self.assertEqual(self._pins_from_share(), 0)

    def test_a_third_account_gets_404_and_the_share_stays_pending(self) -> None:
        response = self.send("post", self.url, {"action": "accept"}, auth=self.stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._status(), PinShareStatus.PENDING)
        self.assertEqual(self._pins_from_share(), 0)

    def test_the_sender_cannot_answer_their_own_share(self) -> None:
        sender_auth = self.key(self.sender_user, self.scopes)

        self.assertEqual(self.send("post", self.url, {"action": "reject"}, auth=sender_auth).status_code, 404)
        self.assertEqual(self._status(), PinShareStatus.PENDING)

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, {"action": "accept"})
        self.assertEqual(self._status(), PinShareStatus.PENDING)

    def test_an_unknown_action_is_400_and_the_share_stays_pending(self) -> None:
        for action in ("maybe", "", 1, None, ["accept"]):
            with self.subTest(action=action):
                self.assertEqual(self.send("post", self.url, {"action": action}).status_code, 400)
        self.assertEqual(self._status(), PinShareStatus.PENDING)

    def test_a_malformed_body_is_4xx_and_the_share_stays_pending(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        self.assertEqual(self._status(), PinShareStatus.PENDING)
        self.assertEqual(self._pins_from_share(), 0)


class PinBulkMergeRouteTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.PINS_READ, ApiKeyScope.PINS_WRITE)
    read_scopes = (ApiKeyScope.PINS_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("external_api:pins.bulk.merge")
        self.target = create_pin_for_profile(self.owner, name="Mill", latitude=42.5, longitude=-73.5).pin
        self.source = create_pin_for_profile(self.owner, name="Mill Annex", latitude=42.6, longitude=-73.6).pin
        self.foreign = create_pin_for_profile(self.stranger, name="Their Pin", latitude=10.0, longitude=10.0).pin

    def _parent(self, pin: Pin) -> int | None:
        return Pin.objects.values_list("parent_pin_id", flat=True).get(pk=pin.pk)

    def _body(self, target: Pin, *sources: Pin) -> dict:
        return {"target_uuid": str(target.uuid), "source_uuids": [str(source.uuid) for source in sources]}

    def test_the_owner_folds_a_pin_under_another(self) -> None:
        response = self.send("post", self.url, self._body(self.target, self.source))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self._parent(self.source), self.target.pk)
        self.assertEqual(response.json()["merged_uuids"], [str(self.source.uuid)])

    def test_another_accounts_target_is_404_and_nothing_moves(self) -> None:
        response = self.send("post", self.url, self._body(self.foreign, self.source))

        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self._parent(self.source))

    def test_another_accounts_pin_is_never_folded_in(self) -> None:
        response = self.send("post", self.url, self._body(self.target, self.foreign))

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self._parent(self.foreign))

    def test_a_stranger_cannot_merge_the_owners_pins(self) -> None:
        response = self.send("post", self.url, self._body(self.target, self.source), auth=self.stranger_auth)

        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self._parent(self.source))

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("post", self.url, self._body(self.target, self.source))
        self.assertIsNone(self._parent(self.source))

    def test_a_malformed_body_is_4xx_and_nothing_moves(self) -> None:
        self.assert_malformed_bodies_are_4xx("post", self.url)
        for body in (
            {"target_uuid": "x", "source_uuids": [str(self.source.uuid)]},
            {"target_uuid": str(self.target.uuid), "source_uuids": str(self.source.uuid)},
            {"target_uuid": str(self.target.uuid), "source_uuids": []},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.send("post", self.url, body).status_code, 400)
        self.assertIsNone(self._parent(self.source))


class TripRsvpRouteTests(ExternalApiRouteCase):
    scopes = (ApiKeyScope.TRIPS_READ, ApiKeyScope.TRIPS_WRITE)
    read_scopes = (ApiKeyScope.TRIPS_READ,)

    def setUp(self) -> None:
        super().setUp()
        self.trip = baker.make(Trip, creator=self.owner, name="Rust Belt Run")
        self.membership = TripMembership.objects.create(trip=self.trip, profile=self.owner)
        self.url = reverse("external_api:trips.rsvp", args=[self.trip.slug])

    def _rsvp(self) -> str | None:
        return TripMembership.objects.values_list("rsvp", flat=True).get(pk=self.membership.pk)

    def test_a_member_sets_and_clears_their_rsvp(self) -> None:
        self.assertEqual(self.send("put", self.url, {"rsvp": "maybe"}).status_code, 200)
        self.assertEqual(self._rsvp(), "maybe")

        self.assertEqual(self.send("put", self.url, {"rsvp": None}).status_code, 200)
        self.assertIsNone(self._rsvp())

    def test_an_invited_member_may_answer_the_invitation(self) -> None:
        invited_user = baker.make("auth.User")
        invited = TripMembership.objects.create(
            trip=self.trip, profile=invited_user.profile, status=TripMembership.STATUS_INVITED
        )

        response = self.send("put", self.url, {"rsvp": "no"}, auth=self.key(invited_user, self.scopes))

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(TripMembership.objects.values_list("rsvp", flat=True).get(pk=invited.pk), "no")

    def test_a_non_member_gets_404_and_no_membership_is_made(self) -> None:
        self.assertEqual(self.send("put", self.url, {"rsvp": "yes"}, auth=self.stranger_auth).status_code, 404)

        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=self.stranger).exists())

    def test_anonymous_and_a_read_only_key_are_refused(self) -> None:
        self.assert_refused_without_credentials("put", self.url, {"rsvp": "yes"})
        self.assertIsNone(self._rsvp())

    def test_an_unknown_answer_is_400_and_changes_nothing(self) -> None:
        self.send("put", self.url, {"rsvp": "yes"})

        for value in ("perhaps", "", 1, ["yes"]):
            with self.subTest(value=value):
                self.assertEqual(self.send("put", self.url, {"rsvp": value}).status_code, 400)
        self.assertEqual(self._rsvp(), "yes")

    def test_a_malformed_body_is_4xx(self) -> None:
        self.assert_malformed_bodies_are_4xx("put", self.url)
        self.assertIsNone(self._rsvp())
