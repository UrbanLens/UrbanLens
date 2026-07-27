"""Tests for the external API's trip surface.

Trips are the first *shared-space* resource this API exposes: unlike pins,
which belong to exactly one person, a trip carries other members' identities,
their comments, and coordinates they may have chosen to reveal only to some
people. So alongside the usual CRUD and scope checks, these tests pin down
three enumeration/authorization defects the extraction deliberately fixed:

1. a missing trip and a trip that isn't yours must be indistinguishable (both 404),
2. member lookups must never leave the trip's own roster,
3. dragging an activity marker must require edit-activities permission, and its
   coordinates must be bounds-checked.

They also assert the trip-map payload is byte-identical between the internal
HTMX endpoint and this one, which is what keeps ``services.trip_map`` an
actual single source rather than two implementations that happen to agree.
"""

from __future__ import annotations

import datetime
from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment, TripMembership
from urbanlens.dashboard.services.api_keys import generate_api_key

_TRIP_SCOPES = [ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _TripApiTestCase(TestCase):
    """Shared fixture: a key owner with trip scopes, plus a second user."""

    def setUp(self) -> None:
        """Create the key owner, a bystander, and a trip-scoped API key."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="owner")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="bystander")
        self.other_profile = Profile.objects.get(user=self.other_user)
        _api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=_api_key.pk).update(scopes=[*_TRIP_SCOPES, ApiKeyScope.PROFILE_READ.value])
        self.api_key = _api_key

    def _key_with_scopes(self, scopes: list[str]) -> str:
        """Issue a second key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(self.user, "Scoped")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _make_trip(self, creator: Profile | None = None, **kwargs) -> Trip:
        """Create a trip with its creator joined, as the create flow would."""
        creator = creator or self.profile
        trip = Trip.objects.create(creator=creator, name=kwargs.pop("name", "Detroit"), **kwargs)
        TripMembership.objects.create(trip=trip, profile=creator, status=TripMembership.STATUS_JOINED, rsvp="yes")
        return trip

    def _get(self, name: str, *args, **kwargs):
        """GET a named external-API route with the fixture's bearer key."""
        return self.client.get(reverse(name, args=args), kwargs.pop("data", None), **_bearer(self.raw_key), **kwargs)


class TripCrudTests(_TripApiTestCase):
    """Creating, reading, updating and deleting a trip through the API."""

    def test_create_returns_201_and_persists(self) -> None:
        """A minimal create makes a real trip owned by the key's user."""
        response = self.client.post(
            reverse("external_api:trips"),
            {"name": "Gary", "description": "Steel town"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)
        trip = Trip.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(trip.name, "Gary")
        self.assertEqual(trip.creator, self.profile)

    def test_blank_name_gets_a_generated_placeholder(self) -> None:
        """Omitting the name is allowed - the trip still gets a real one."""
        response = self.client.post(reverse("external_api:trips"), {}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["name"])

    def test_uuid_replay_returns_200_and_no_duplicate(self) -> None:
        """Re-sending the same client uuid answers with the original trip."""
        client_uuid = str(uuid4())
        first = self.client.post(reverse("external_api:trips"), {"name": "Gary", "uuid": client_uuid}, content_type="application/json", **_bearer(self.raw_key))
        second = self.client.post(reverse("external_api:trips"), {"name": "Gary", "uuid": client_uuid}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["uuid"], second.json()["uuid"])
        self.assertEqual(Trip.objects.filter(uuid=client_uuid).count(), 1)

    def test_another_users_uuid_is_refused(self) -> None:
        """A caller cannot replay - or hijack - a uuid belonging to someone else's trip."""
        theirs = self._make_trip(creator=self.other_profile)
        response = self.client.post(reverse("external_api:trips"), {"name": "Mine", "uuid": str(theirs.uuid)}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_upcoming_trip_quota_is_enforced(self) -> None:
        """The site's max-upcoming-trips cap answers 400, not a 500.

        The existing trip needs a future start date to actually count: an
        undated trip with no scheduled activities is not "upcoming" (see
        ``TripQuerySet.upcoming``), so it consumes no quota.
        """
        settings_row = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_row.pk).update(max_upcoming_trips_per_user=1)
        self._make_trip(name="Already planning", start_date=datetime.date.today() + datetime.timedelta(days=7))
        response = self.client.post(reverse("external_api:trips"), {"name": "One too many"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum", response.json()["error"])

    def test_end_date_before_start_date_is_rejected(self) -> None:
        """An incoherent date range never reaches the service."""
        response = self.client.post(
            reverse("external_api:trips"),
            {"name": "Backwards", "start_date": "2026-05-02", "end_date": "2026-05-01"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_detail_bundles_viewer_permissions_and_members(self) -> None:
        """One GET is enough to render the trip screen."""
        trip = self._make_trip()
        response = self._get("external_api:trips.detail", trip.slug)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["viewer"]["is_creator"])
        self.assertTrue(body["viewer"]["can_add_activities"])
        self.assertEqual(len(body["members"]), 1)
        self.assertIn("calendar_sync", body)
        self.assertIn("permissions", body)

    def test_patch_is_presence_keyed(self) -> None:
        """An omitted field is untouched; a blank description clears."""
        trip = self._make_trip(description="Original")
        response = self.client.patch(
            reverse("external_api:trips.detail", args=[trip.slug]),
            {"description": ""},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        trip.refresh_from_db()
        self.assertEqual(trip.name, "Detroit")
        self.assertIsNone(trip.description)

    def test_patch_blank_name_is_ignored(self) -> None:
        """A trip always keeps a name - blank is a no-op, not a wipe."""
        trip = self._make_trip()
        self.client.patch(reverse("external_api:trips.detail", args=[trip.slug]), {"name": "  "}, content_type="application/json", **_bearer(self.raw_key))
        trip.refresh_from_db()
        self.assertEqual(trip.name, "Detroit")

    def test_delete_is_creator_only_and_stashes_for_undo(self) -> None:
        """Deleting goes through the Undo framework, and only the creator may."""
        trip = self._make_trip()
        with mock.patch("urbanlens.dashboard.services.undo.service.stash_for_undo") as stash:
            response = self.client.delete(reverse("external_api:trips.detail", args=[trip.slug]), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Trip.objects.filter(pk=trip.pk).exists())
        stash.assert_called_once()

    def test_member_who_is_not_creator_cannot_delete(self) -> None:
        """A joined member gets 403, not a silent delete."""
        trip = self._make_trip(creator=self.other_profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        response = self.client.delete(reverse("external_api:trips.detail", args=[trip.slug]), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_list_is_paginated(self) -> None:
        """The list endpoint uses the shared page envelope."""
        self._make_trip(name="A")
        self._make_trip(name="B")
        response = self._get("external_api:trips")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 2)
        self.assertIn("results", body)


class TripScopeEnforcementTests(_TripApiTestCase):
    """Trip data is unreachable without the trip scopes specifically."""

    def test_post_without_trips_write_is_forbidden(self) -> None:
        """Read access alone must not permit creating a trip."""
        raw = self._key_with_scopes([ApiKeyScope.TRIPS_READ.value])
        response = self.client.post(reverse("external_api:trips"), {"name": "Nope"}, content_type="application/json", **_bearer(raw))
        self.assertEqual(response.status_code, 403)

    def test_get_without_any_trip_scope_is_forbidden(self) -> None:
        """A key holding only pin scopes cannot read trips."""
        raw = self._key_with_scopes([ApiKeyScope.PINS_READ.value, ApiKeyScope.PINS_WRITE.value])
        response = self.client.get(reverse("external_api:trips"), **_bearer(raw))
        self.assertEqual(response.status_code, 403)

    def test_default_issued_key_cannot_reach_trips(self) -> None:
        """A key issued today gets the original four scopes - none of them trips.

        The whole point of leaving ``trips:*`` out of
        ``_default_api_key_scopes``: every already-issued key would otherwise
        have silently gained access to other people's trip data.
        """
        _api_key, raw = generate_api_key(self.user, "Default grant")
        response = self.client.get(reverse("external_api:trips"), **_bearer(raw))
        self.assertEqual(response.status_code, 403)

    def test_write_endpoints_land_in_the_write_throttle_tier(self) -> None:
        """``trips:write`` ends in ':write', so the tiered throttle classifies it."""
        from urbanlens.dashboard.external_api.throttling import TIER_READ, TIER_WRITE, request_tier
        from urbanlens.dashboard.external_api.views import TripsView

        self.assertEqual(request_tier(TripsView, "POST"), TIER_WRITE)
        self.assertEqual(request_tier(TripsView, "GET"), TIER_READ)


class TripExistenceEnumerationTests(_TripApiTestCase):
    """A trip that isn't yours must look exactly like one that doesn't exist."""

    def test_missing_and_forbidden_trips_answer_identically(self) -> None:
        """Regression guard: these used to differ 404 vs 403, which leaked slugs."""
        theirs = self._make_trip(creator=self.other_profile, name="Private")
        missing = self._get("external_api:trips.detail", "no-such-trip-slug")
        forbidden = self._get("external_api:trips.detail", theirs.slug)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(missing.json(), forbidden.json())

    def test_invited_member_may_still_read_the_trip(self) -> None:
        """Viewing is gated on membership, not on having accepted the invite."""
        trip = self._make_trip(creator=self.other_profile)
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_INVITED)
        response = self._get("external_api:trips.detail", trip.slug)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["viewer"]["has_joined"])


class TripMemberTests(_TripApiTestCase):
    """Inviting, removing and promoting members, and the roster-scoped lookup."""

    def setUp(self) -> None:
        """Add a trip the key owner created and a third user to invite."""
        super().setUp()
        self.trip = self._make_trip()
        self.trip.allow_add_members = Trip.PERM_EVERYONE
        self.trip.save(update_fields=["allow_add_members"])
        self.invitee_user = baker.make(User, username="invitee")
        self.invitee = Profile.objects.get(user=self.invitee_user)

    def _add(self, username: str):
        """POST an add-member submission for *username*."""
        return self.client.post(
            reverse("external_api:trips.members", args=[self.trip.slug]),
            {"username": username},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

    def test_add_member_invites_and_is_idempotent(self) -> None:
        """A re-invite answers 200 with no second membership row."""
        first = self._add("invitee")
        second = self._add("invitee")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(TripMembership.objects.filter(trip=self.trip, profile=self.invitee).count(), 1)

    def test_unknown_username_is_404_with_the_raw_name_in_json(self) -> None:
        """The JSON surface reports the submitted name unescaped, not HTML-encoded."""
        response = self._add("no-such-person")
        self.assertEqual(response.status_code, 404)
        self.assertIn("no-such-person", response.json()["error"])

    def test_blocked_user_cannot_be_invited(self) -> None:
        """A block stops a forced membership the same way it stops a DM."""
        with mock.patch.object(Profile, "are_blocked", return_value=True):
            response = self._add("invitee")
        self.assertEqual(response.status_code, 403)

    def test_member_cap_is_enforced(self) -> None:
        """Hitting max_trip_members answers 400 rather than over-filling the trip."""
        settings_row = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_row.pk).update(max_trip_members=1)
        response = self._add("invitee")
        self.assertEqual(response.status_code, 400)
        self.assertIn("full", response.json()["error"])

    def test_member_without_permission_cannot_invite(self) -> None:
        """The trip's allow_add_members level is honored."""
        self.trip.allow_add_members = Trip.PERM_NONE
        self.trip.save(update_fields=["allow_add_members"])
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        response = self.client.post(
            reverse("external_api:trips.members", args=[trip.slug]),
            {"username": "invitee"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 403)

    def test_member_lookup_never_leaves_the_trip(self) -> None:
        """Regression guard for the global-profile enumeration defect.

        The member endpoints used to resolve the target with a site-wide
        ``get_object_or_404(Profile, pk=...)`` before narrowing to the trip, so
        the status code told a caller whether an arbitrary profile existed.
        A real profile that simply isn't on this trip is now a plain 404.
        """
        outsider = baker.make(User, username="outsider")
        outsider_profile = Profile.objects.get(user=outsider)
        self.assertIsNotNone(outsider_profile.slug)
        response = self.client.delete(
            reverse("external_api:trips.members.detail", args=[self.trip.slug, outsider_profile.slug]),
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 404)

    def test_remove_member_by_slug(self) -> None:
        """The creator can remove a member addressed by their profile slug."""
        TripMembership.objects.create(trip=self.trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        response = self.client.delete(
            reverse("external_api:trips.members.detail", args=[self.trip.slug, self.invitee.slug]),
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=self.invitee).exists())

    def test_removing_a_member_revokes_their_calendar_sync(self) -> None:
        """A removed member's live calendar export must be cut at the same moment."""
        TripMembership.objects.create(trip=self.trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        with mock.patch("urbanlens.dashboard.services.trip_membership.disconnect_member_calendar_sync") as disconnect:
            self.client.delete(reverse("external_api:trips.members.detail", args=[self.trip.slug, self.invitee.slug]), **_bearer(self.raw_key))
        disconnect.assert_called_once()

    def test_creator_cannot_be_removed(self) -> None:
        """Removing the creator is a 400, not a silently broken trip."""
        response = self.client.delete(
            reverse("external_api:trips.members.detail", args=[self.trip.slug, self.profile.slug]),
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_creator_cannot_leave(self) -> None:
        """The creator must delete the trip rather than abandon it."""
        response = self.client.delete(reverse("external_api:trips.leave", args=[self.trip.slug]), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 400)

    def test_organizer_flag_is_explicit_not_a_toggle(self) -> None:
        """Sending the same value twice leaves the flag where it was."""
        membership = TripMembership.objects.create(trip=self.trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        url = reverse("external_api:trips.members.detail", args=[self.trip.slug, self.invitee.slug])
        self.client.patch(url, {"is_organizer": True}, content_type="application/json", **_bearer(self.raw_key))
        self.client.patch(url, {"is_organizer": True}, content_type="application/json", **_bearer(self.raw_key))
        membership.refresh_from_db()
        self.assertTrue(membership.is_organizer)

    def test_creator_is_always_an_organizer(self) -> None:
        """Trying to demote the creator is a 400."""
        response = self.client.patch(
            reverse("external_api:trips.members.detail", args=[self.trip.slug, self.profile.slug]),
            {"is_organizer": False},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_joining_records_share_provenance(self) -> None:
        """Joining reveals the itinerary, which must enter the reshare chain."""
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_INVITED)
        with mock.patch("urbanlens.dashboard.services.trip_share_tracking.record_trip_shares_for_member") as record:
            response = self.client.post(reverse("external_api:trips.join", args=[trip.slug]), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        record.assert_called_once()
        self.assertTrue(TripMembership.objects.get(trip=trip, profile=self.profile).status == TripMembership.STATUS_JOINED)

    def test_masked_member_exposes_no_slug(self) -> None:
        """A member this viewer may not identify is addressable only by uuid.

        The profile slug is derived from the username, so emitting it would
        undo the display-name masking entirely.
        """
        trip = self._make_trip(creator=self.other_profile, name="Shared")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        Profile.objects.filter(pk=self.other_profile.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        response = self._get("external_api:trips.members", trip.slug)
        self.assertEqual(response.status_code, 200)
        creator_row = next(row for row in response.json()["results"] if row["is_creator"])
        self.assertIsNone(creator_row["profile"]["slug"])
        self.assertIsNotNone(creator_row["profile"]["uuid"])


class TripActivityTests(_TripApiTestCase):
    """Activity CRUD, the reposition authorization fix, and coordinate visibility."""

    def setUp(self) -> None:
        """Create a trip owned by the key user with one located activity."""
        super().setUp()
        self.trip = self._make_trip()
        self.location = Location.objects.create(latitude=42.33, longitude=-83.04, official_name="Packard")
        self.activity = TripActivity.objects.create(trip=self.trip, location=self.location, added_by=self.profile, title="Packard Plant")

    def test_create_activity_records_share_provenance(self) -> None:
        """Putting a place on the itinerary counts as a share of that place."""
        with mock.patch("urbanlens.dashboard.services.trip_share_tracking.record_trip_activity_shares") as record:
            response = self.client.post(
                reverse("external_api:trips.activities", args=[self.trip.slug]),
                {"title": "Fisher Body", "latitude": 42.36, "longitude": -83.08},
                content_type="application/json",
                **_bearer(self.raw_key),
            )
        self.assertEqual(response.status_code, 201)
        record.assert_called_once()

    def test_activity_quota_is_enforced(self) -> None:
        """max_trip_activities answers 400, not an unbounded itinerary."""
        settings_row = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings_row.pk).update(max_trip_activities=1)
        response = self.client.post(
            reverse("external_api:trips.activities", args=[self.trip.slug]),
            {"title": "One too many"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_notes_over_the_limit_are_a_400_not_a_500(self) -> None:
        """Text limits are checked before save, so no ValidationError escapes."""
        response = self.client.post(
            reverse("external_api:trips.activities", args=[self.trip.slug]),
            {"title": "Long", "notes": "x" * 60_000},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_list_does_not_route_legs_by_default(self) -> None:
        """Regression guard: a plain list fetch must never make an OSRM call."""
        TripActivity.objects.create(
            trip=self.trip,
            location=Location.objects.create(latitude=42.4, longitude=-83.1, official_name="Second"),
            added_by=self.profile,
            title="Second stop",
        )
        with mock.patch("urbanlens.dashboard.services.trip_legs.compute_legs") as compute:
            response = self._get("external_api:trips.activities", self.trip.slug)
        self.assertEqual(response.status_code, 200)
        compute.assert_not_called()
        self.assertTrue(all(row["leg"] is None for row in response.json()["results"]))

    def test_include_legs_opts_into_routing(self) -> None:
        """Asking for legs explicitly is what triggers the routing lookup."""
        TripActivity.objects.create(
            trip=self.trip,
            location=Location.objects.create(latitude=42.4, longitude=-83.1, official_name="Second"),
            added_by=self.profile,
            title="Second stop",
        )
        Profile.objects.filter(pk=self.profile.pk).update(external_apis_enabled=True)
        with mock.patch("urbanlens.dashboard.services.trip_legs.compute_legs", return_value={}) as compute:
            response = self.client.get(
                reverse("external_api:trips.activities", args=[self.trip.slug]),
                {"include_legs": "true"},
                **_bearer(self.raw_key),
            )
        self.assertEqual(response.status_code, 200)
        compute.assert_called_once()

    def test_hidden_location_omits_coordinates_entirely(self) -> None:
        """A hidden location must be null, not merely flagged."""
        TripActivity.objects.filter(pk=self.activity.pk).update(location_hidden=True)
        response = self._get("external_api:trips.activities", self.trip.slug)
        row = response.json()["results"][0]
        self.assertTrue(row["location_hidden"])
        self.assertIsNone(row["latitude"])
        self.assertIsNone(row["longitude"])

    def test_position_requires_edit_permission(self) -> None:
        """Regression guard: an invited-not-joined member could move any marker.

        The reposition endpoint used to check trip *membership* only, so anyone
        who had merely been invited could drag any activity on the map.
        """
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        activity = TripActivity.objects.create(trip=trip, location=self.location, added_by=self.other_profile, title="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_INVITED)
        response = self.client.post(
            reverse("external_api:trips.activities.position", args=[trip.slug, activity.id]),
            {"lat": 1.0, "lng": 2.0},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 403)
        activity.refresh_from_db()
        self.assertIsNone(activity.lat_override)

    def test_position_rejects_out_of_range_coordinates(self) -> None:
        """Regression guard: any float used to be accepted and persisted."""
        response = self.client.post(
            reverse("external_api:trips.activities.position", args=[self.trip.slug, self.activity.id]),
            {"lat": 5000, "lng": 2.0},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)
        self.activity.refresh_from_db()
        self.assertIsNone(self.activity.lat_override)

    def test_position_saves_valid_coordinates(self) -> None:
        """A permitted, in-range drag is persisted as an override only."""
        response = self.client.post(
            reverse("external_api:trips.activities.position", args=[self.trip.slug, self.activity.id]),
            {"lat": 42.5, "lng": -83.5},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 200)
        self.activity.refresh_from_db()
        self.assertAlmostEqual(self.activity.lat_override, 42.5)
        # The underlying Location is never moved by a marker drag.
        self.location.refresh_from_db()
        self.assertAlmostEqual(float(self.location.latitude), 42.33)

    def test_vote_is_idempotent(self) -> None:
        """Sending the same vote twice leaves one vote, not zero."""
        url = reverse("external_api:trips.activities.vote", args=[self.trip.slug, self.activity.id])
        self.client.put(url, {"vote": "up"}, content_type="application/json", **_bearer(self.raw_key))
        response = self.client.put(url, {"vote": "up"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_vote"], "up")
        self.assertEqual(response.json()["vote_up"], 1)

    def test_vote_null_clears(self) -> None:
        """A null vote removes the caller's vote."""
        url = reverse("external_api:trips.activities.vote", args=[self.trip.slug, self.activity.id])
        self.client.put(url, {"vote": "up"}, content_type="application/json", **_bearer(self.raw_key))
        response = self.client.put(url, {"vote": None}, content_type="application/json", **_bearer(self.raw_key))
        self.assertIsNone(response.json()["user_vote"])

    def test_activity_rsvp_override_and_clear(self) -> None:
        """Setting an override marks it as such; clearing falls back to the trip RSVP."""
        url = reverse("external_api:trips.activities.rsvp", args=[self.trip.slug, self.activity.id])
        set_response = self.client.put(url, {"rsvp": "no"}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(set_response.json()["rsvp"], "no")
        self.assertTrue(set_response.json()["rsvp_is_override"])
        clear_response = self.client.put(url, {"rsvp": None}, content_type="application/json", **_bearer(self.raw_key))
        self.assertFalse(clear_response.json()["rsvp_is_override"])
        # Falls back to the membership's own "yes" from the fixture.
        self.assertEqual(clear_response.json()["rsvp"], "yes")

    def test_delete_activity(self) -> None:
        """A permitted delete removes the row and answers 204."""
        response = self.client.delete(
            reverse("external_api:trips.activities.detail", args=[self.trip.slug, self.activity.id]),
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(TripActivity.objects.filter(pk=self.activity.pk).exists())

    def test_unknown_activity_is_404(self) -> None:
        """An id that isn't on this trip is not found."""
        response = self.client.delete(
            reverse("external_api:trips.activities.detail", args=[self.trip.slug, 999_999]),
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 404)

    def test_status_completed_routes_through_complete_activity(self) -> None:
        """Completing through the API logs visits like the web app's own action."""
        with mock.patch("urbanlens.dashboard.services.trip_activities.create_visit_entries_for_completed_activity") as entries:
            response = self.client.put(
                reverse("external_api:trips.activities.status", args=[self.trip.slug, self.activity.id]),
                {"status": "completed"},
                content_type="application/json",
                **_bearer(self.raw_key),
            )
        self.assertEqual(response.status_code, 200)
        entries.assert_called_once()
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, TripActivity.STATUS_COMPLETED)


class TripCommentTests(_TripApiTestCase):
    """Comment visibility gates, reactions, replies and deletion rights."""

    def setUp(self) -> None:
        """Create a trip with the key owner and a second member on it."""
        super().setUp()
        self.trip = self._make_trip()
        TripMembership.objects.create(trip=self.trip, profile=self.other_profile, status=TripMembership.STATUS_JOINED)

    def test_post_and_read_a_comment(self) -> None:
        """A posted comment comes back rendered, with the caller able to delete it."""
        response = self.client.post(
            reverse("external_api:trips.comments", args=[self.trip.slug]),
            {"text": "Bring a torch"},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("Bring a torch", body["rendered_html"])
        self.assertTrue(body["can_delete"])

    def test_blank_comment_is_rejected(self) -> None:
        """An empty submission never creates a row."""
        response = self.client.post(
            reverse("external_api:trips.comments", args=[self.trip.slug]),
            {"text": "   "},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_reply_nests_under_its_parent(self) -> None:
        """Replies come back inside their parent, not as separate top-level rows."""
        parent = TripComment.objects.create(trip=self.trip, author=self.profile, text="Parent")
        TripComment.objects.create(trip=self.trip, author=self.other_profile, text="Reply", parent=parent)
        response = self._get("external_api:trips.comments", self.trip.slug)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(body["results"][0]["replies"]), 1)
        self.assertEqual(body["results"][0]["replies"][0]["text"], "Reply")

    def test_comment_visibility_gate_hides_the_whole_comment(self) -> None:
        """An author whose comment_visibility excludes this viewer is filtered out."""
        TripComment.objects.create(trip=self.trip, author=self.other_profile, text="Hidden")
        with mock.patch.object(Profile, "can_view_comments_from", return_value=False):
            response = self._get("external_api:trips.comments", self.trip.slug)
        self.assertEqual(response.json()["count"], 0)

    def test_pending_scan_hides_a_comment_from_everyone_but_its_author(self) -> None:
        """An unscanned image keeps the comment private until the scan clears."""
        TripComment.objects.create(trip=self.trip, author=self.other_profile, text="Scanning", pending_scan=True)
        response = self._get("external_api:trips.comments", self.trip.slug)
        self.assertEqual(response.json()["count"], 0)

    def test_can_delete_is_false_for_someone_elses_comment(self) -> None:
        """A member sees the trip creator's comment but cannot delete it..."""
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)
        comment = TripComment.objects.create(trip=trip, author=self.other_profile, text="Theirs")
        response = self._get("external_api:trips.comments", trip.slug)
        self.assertFalse(response.json()["results"][0]["can_delete"])
        deletion = self.client.delete(
            reverse("external_api:trips.comments.detail", args=[trip.slug, comment.id]),
            **_bearer(self.raw_key),
        )
        self.assertEqual(deletion.status_code, 403)

    def test_reaction_is_idempotent(self) -> None:
        """Setting the same reaction twice leaves exactly one."""
        comment = TripComment.objects.create(trip=self.trip, author=self.profile, text="React to me")
        url = reverse("external_api:trips.comments.reactions", args=[self.trip.slug, comment.id])
        self.client.put(url, {"emoji": "🔥", "reacted": True}, content_type="application/json", **_bearer(self.raw_key))
        response = self.client.put(url, {"emoji": "🔥", "reacted": True}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        reactions = {row["emoji"]: row for row in response.json()["reactions"]}
        self.assertEqual(reactions["🔥"]["count"], 1)
        self.assertTrue(reactions["🔥"]["reacted"])

    def test_reaction_can_be_removed(self) -> None:
        """``reacted: false`` removes the caller's reaction."""
        comment = TripComment.objects.create(trip=self.trip, author=self.profile, text="React to me")
        url = reverse("external_api:trips.comments.reactions", args=[self.trip.slug, comment.id])
        self.client.put(url, {"emoji": "🔥", "reacted": True}, content_type="application/json", **_bearer(self.raw_key))
        response = self.client.put(url, {"emoji": "🔥", "reacted": False}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.json()["reactions"], [])

    def test_unknown_emoji_is_rejected(self) -> None:
        """Only the allowed reaction set is accepted."""
        comment = TripComment.objects.create(trip=self.trip, author=self.profile, text="React to me")
        response = self.client.put(
            reverse("external_api:trips.comments.reactions", args=[self.trip.slug, comment.id]),
            {"emoji": "🦆", "reacted": True},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)


class TripMapParityTests(_TripApiTestCase):
    """The external map payload must equal the internal one, byte for byte.

    This is the regression guard for ``services.trip_map`` being a genuine
    single source: if either surface ever grows its own point-building code,
    these payloads drift and this test fails.
    """

    def setUp(self) -> None:
        """Build a trip exercising numbering, a completed stop, and a child trip."""
        super().setUp()
        self.trip = self._make_trip()
        self.child = self._make_trip(name="Side quest")
        for index, (lat, lng, status) in enumerate(
            [
                (42.33, -83.04, TripActivity.STATUS_PROPOSED),
                (42.36, -83.08, TripActivity.STATUS_CONFIRMED),
                (42.40, -83.10, TripActivity.STATUS_COMPLETED),
            ],
        ):
            TripActivity.objects.create(
                trip=self.trip,
                location=Location.objects.create(latitude=lat, longitude=lng, official_name=f"Stop {index}"),
                added_by=self.profile,
                title=f"Stop {index}",
                status=status,
            )
        TripActivity.objects.create(
            trip=self.child,
            location=Location.objects.create(latitude=42.5, longitude=-83.2, official_name="Child stop"),
            added_by=self.profile,
            title="Child stop",
        )
        TripActivity.objects.create(trip=self.trip, added_by=self.profile, title="Nested", child_trip=self.child)

    def _payloads(self, include_past: str) -> tuple[dict, dict]:
        """Fetch the internal and external map payloads for the same fixture."""
        self.client.force_login(self.user)
        internal = self.client.get(reverse("trips.map_data", args=[self.trip.slug]), {"include_past": include_past})
        self.client.logout()
        external = self.client.get(
            reverse("external_api:trips.map", args=[self.trip.slug]),
            {"include_past": include_past},
            **_bearer(self.raw_key),
        )
        self.assertEqual(internal.status_code, 200)
        self.assertEqual(external.status_code, 200)
        return internal.json(), external.json()

    def test_payloads_are_identical(self) -> None:
        """Same fixture, same viewer, same points - in the same order."""
        internal, external = self._payloads("0")
        self.assertEqual(internal["points"], external["points"])

    def test_payloads_are_identical_with_past_included(self) -> None:
        """``include_past`` behaves the same on both surfaces."""
        internal, external = self._payloads("1")
        self.assertEqual(internal["points"], external["points"])
        self.assertGreater(len(external["points"]), len(self._payloads("0")[1]["points"]))

    def test_ghost_markers_are_undraggable_and_unnumbered(self) -> None:
        """A child trip's stops are context, not this trip's own itinerary."""
        _internal, external = self._payloads("0")
        ghosts = [point for point in external["points"] if point.get("child_trip")]
        self.assertTrue(ghosts)
        for ghost in ghosts:
            self.assertIsNone(ghost["index"])
            self.assertIsNone(ghost["activity_id"])
            self.assertFalse(ghost["draggable"])

    def test_map_is_not_paginated(self) -> None:
        """The client needs every point at once to fit the map's bounds."""
        _internal, external = self._payloads("0")
        self.assertNotIn("results", external)
        self.assertIn("points", external)
