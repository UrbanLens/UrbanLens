"""Routes that change who runs a trip, who is on it, and what a list copies into it."""

from __future__ import annotations

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripMembership


def _cap_trip_activities(limit: int) -> None:
    site = SiteSettings.get_current()
    site.max_trip_activities = limit
    site.save()


class _TripFixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.creator_user = baker.make(User)
        self.creator = self.creator_user.profile
        self.member_user = baker.make(User)
        self.member = self.member_user.profile
        self.stranger_user = baker.make(User)
        self.trip = baker.make(Trip, creator=self.creator, name="Rust Belt")
        TripMembership.objects.create(trip=self.trip, profile=self.creator, status=TripMembership.STATUS_JOINED)
        self.membership = TripMembership.objects.create(
            trip=self.trip, profile=self.member, status=TripMembership.STATUS_JOINED
        )

    def assert_login_redirect(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(settings.LOGIN_URL), response["Location"])

    def _is_organizer(self) -> bool:
        self.membership.refresh_from_db()
        return self.membership.is_organizer


class TripMemberOrganizerRouteTests(_TripFixture):
    def setUp(self) -> None:
        super().setUp()
        self.url = reverse("trips.member.organizer", args=[self.trip.slug, self.member.pk])

    def test_creator_promotes_then_demotes(self) -> None:
        self.client.force_login(self.creator_user)

        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertTrue(self._is_organizer())
        self.assertEqual(self.client.post(self.url).status_code, 200)
        self.assertFalse(self._is_organizer())

    def test_an_organizer_who_is_not_the_creator_cannot_promote_anyone(self) -> None:
        other_user = baker.make(User)
        other = TripMembership.objects.create(
            trip=self.trip, profile=other_user.profile, status=TripMembership.STATUS_JOINED
        )
        self.membership.is_organizer = True
        self.membership.save()
        self.client.force_login(self.member_user)

        response = self.client.post(reverse("trips.member.organizer", args=[self.trip.slug, other_user.profile.pk]))

        self.assertEqual(response.status_code, 403)
        other.refresh_from_db()
        self.assertFalse(other.is_organizer)

    def test_a_member_cannot_promote_themselves(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(self._is_organizer())

    def test_non_member_gets_404(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self._is_organizer())

    def test_a_profile_not_on_this_trip_is_404_not_created(self) -> None:
        self.client.force_login(self.creator_user)

        response = self.client.post(
            reverse("trips.member.organizer", args=[self.trip.slug, self.stranger_user.profile.pk])
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=self.stranger_user.profile).exists())

    def test_the_creator_cannot_be_demoted(self) -> None:
        self.client.force_login(self.creator_user)

        response = self.client.post(reverse("trips.member.organizer", args=[self.trip.slug, self.creator.pk]))

        self.assertEqual(response.status_code, 400)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertFalse(self._is_organizer())


class TripJoinRouteTests(_TripFixture):
    def setUp(self) -> None:
        super().setUp()
        self.membership.status = TripMembership.STATUS_INVITED
        self.membership.save()
        self.url = reverse("trips.join", args=[self.trip.slug])

    def _status(self) -> str:
        self.membership.refresh_from_db()
        return self.membership.status

    def test_an_invited_member_joins(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._status(), TripMembership.STATUS_JOINED)

    def test_an_uninvited_user_gets_404_and_no_membership_is_created(self) -> None:
        self.client.force_login(self.stranger_user)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, 404)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=self.stranger_user.profile).exists())

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self.client.post(self.url))
        self.assertEqual(self._status(), TripMembership.STATUS_INVITED)


class PinListAddToTripRouteTests(_TripFixture):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.member, name="Old Mill")
        self.pin_list = baker.make(PinList, profile=self.member, name="Weekend")
        baker.make(PinListItem, pin_list=self.pin_list, pin=self.pin)
        self.url = reverse("lists.add_to_trip", args=[self.pin_list.slug])

    def _post(self, body):
        return self.client.post(self.url, body, content_type="application/json")

    def _activity_count(self) -> int:
        return TripActivity.objects.filter(trip=self.trip).count()

    def test_a_joined_member_copies_their_list_onto_the_trip(self) -> None:
        self.client.force_login(self.member_user)

        response = self._post({"trip_slug": self.trip.slug})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(TripActivity.objects.filter(trip=self.trip).values_list("pin_id", flat=True)), [self.pin.pk]
        )

    def test_someone_elses_list_is_404_and_nothing_is_copied(self) -> None:
        self.client.force_login(self.creator_user)

        response = self._post({"trip_slug": self.trip.slug})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._activity_count(), 0)

    def test_a_trip_the_user_is_not_on_is_404_and_nothing_is_copied(self) -> None:
        foreign_trip = baker.make(Trip, creator=self.stranger_user.profile)
        self.client.force_login(self.member_user)

        response = self._post({"trip_slug": foreign_trip.slug})

        self.assertEqual(response.status_code, 404)
        self.assertFalse(TripActivity.objects.filter(trip=foreign_trip).exists())

    def test_a_missing_trip_slug_is_a_400(self) -> None:
        self.client.force_login(self.member_user)

        self.assertEqual(self._post({}).status_code, 400)
        self.assertEqual(self._activity_count(), 0)

    def test_anonymous_is_redirected_to_login(self) -> None:
        self.assert_login_redirect(self._post({"trip_slug": self.trip.slug}))
        self.assertEqual(self._activity_count(), 0)

    def test_a_member_the_trip_forbids_from_adding_activities_is_refused(self) -> None:
        self.trip.allow_add_activities = Trip.PERM_NONE
        self.trip.save()
        self.client.force_login(self.member_user)

        response = self._post({"trip_slug": self.trip.slug})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._activity_count(), 0)

    def test_an_invited_member_who_has_not_joined_is_refused(self) -> None:
        self.membership.status = TripMembership.STATUS_INVITED
        self.membership.save()
        self.client.force_login(self.member_user)

        response = self._post({"trip_slug": self.trip.slug})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._activity_count(), 0)

    def test_a_json_array_body_is_a_4xx(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.post(self.url, data="[]", content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))

    def test_a_copy_past_the_activity_limit_is_a_400_and_copies_nothing(self) -> None:
        _cap_trip_activities(1)
        TripActivity.objects.create(trip=self.trip, added_by=self.creator, title="Existing", order=0)
        self.client.force_login(self.member_user)

        response = self._post({"trip_slug": self.trip.slug})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._activity_count(), 1)


class PinListCreateTripRouteTests(_TripFixture):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.member, name="Old Mill")
        self.pin_list = baker.make(PinList, profile=self.member, name="Weekend")
        baker.make(PinListItem, pin_list=self.pin_list, pin=self.pin)
        self.url = reverse("lists.create_trip", args=[self.pin_list.slug])

    def _post(self, body):
        return self.client.post(self.url, body, content_type="application/json")

    def test_owner_gets_a_new_trip_they_created_and_joined_holding_the_lists_pins(self) -> None:
        self.client.force_login(self.member_user)

        response = self._post({"name": "From my list"})

        self.assertEqual(response.status_code, 200)
        trip = Trip.objects.get(name="From my list")
        self.assertEqual(trip.creator_id, self.member.pk)
        self.assertTrue(
            TripMembership.objects.filter(trip=trip, profile=self.member, status=TripMembership.STATUS_JOINED).exists()
        )
        self.assertEqual(list(trip.activities.values_list("pin_id", flat=True)), [self.pin.pk])

    def test_someone_elses_list_is_404_and_no_trip_is_created(self) -> None:
        self.client.force_login(self.creator_user)
        before = Trip.objects.count()

        response = self._post({"name": "Stolen"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Trip.objects.count(), before)

    def test_an_over_long_name_is_a_400_and_no_trip_is_created(self) -> None:
        self.client.force_login(self.member_user)
        before = Trip.objects.count()

        response = self._post({"name": "x" * 1000})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Trip.objects.count(), before)

    def test_anonymous_is_redirected_to_login(self) -> None:
        before = Trip.objects.count()
        self.assert_login_redirect(self._post({"name": "x"}))
        self.assertEqual(Trip.objects.count(), before)

    def test_a_json_array_body_is_a_4xx(self) -> None:
        self.client.force_login(self.member_user)

        response = self.client.post(self.url, data="[]", content_type="application/json")

        self.assertIn(response.status_code, range(400, 500))

    def test_a_list_longer_than_the_activity_limit_is_a_400_and_no_trip_is_created(self) -> None:
        _cap_trip_activities(1)
        baker.make(PinListItem, pin_list=self.pin_list, pin=baker.make(Pin, profile=self.member, name="Water Tower"))
        self.client.force_login(self.member_user)
        before = Trip.objects.count()

        response = self._post({"name": "Too long"})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Trip.objects.count(), before)
