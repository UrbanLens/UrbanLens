"""Tests for the trip *settings* write path, service and endpoint.

A trip's four permission levels decide who on a shared trip may add members,
add or edit activities, and comment. Until now the only editor was the site's
own ``POST /trips/<slug>/settings/`` form, which always submits all four radio
groups at once - so ``services.trips.trip_crud.set_trip_permissions`` was written to
read all four unconditionally and, for any it did not find, fall back to a
*hardcoded default*.

That is invisible while the only caller is a form that always sends
everything, and catastrophic the moment a partial writer exists: a mobile
client toggling one switch would silently rewrite the other three on a trip
other people share. So the first class here pins the service's presence-keyed
contract, and the rest cover the ``PATCH /trips/{slug}/settings/`` endpoint
built on top of it.

The endpoint's one deliberate departure from this API's 404-not-403 rule is
asserted too: a *member* who is not an organizer gets 403, because they have
already been shown the trip and the status therefore leaks nothing. A
non-member still gets 404.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import given, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.trips.trip_crud import TRIP_PERMISSION_FIELDS, set_trip_permissions
from urbanlens.dashboard.services.trips.trip_errors import TripPermissionError, TripValidationError

_TRIP_SCOPES = [ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]

#: The three levels every permission field accepts.
_LEVELS = [Trip.PERM_NONE, Trip.PERM_ORGANIZERS, Trip.PERM_EVERYONE]


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token.

    Args:
        raw_key: The raw (unhashed) API key value.

    Returns:
        Extra kwargs for Django's test client.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _TripSettingsTestCase(TestCase):
    """Shared fixture: a key owner with trip scopes, a bystander, and a trip."""

    def setUp(self) -> None:
        """Create the key owner, a second user, a trip-scoped key, and a trip."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User, username="settings-owner")
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User, username="settings-bystander")
        self.other_profile = Profile.objects.get(user=self.other_user)
        api_key, self.raw_key = generate_api_key(self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[*_TRIP_SCOPES, ApiKeyScope.PROFILE_READ.value])
        self.trip = self._make_trip()

    def _make_trip(self, creator: Profile | None = None, **kwargs) -> Trip:
        """Create a trip with its creator joined, as the create flow would.

        Args:
            creator: The trip's creator; defaults to the key owner.
            **kwargs: Extra ``Trip`` field values.

        Returns:
            The saved trip.
        """
        creator = creator or self.profile
        trip = Trip.objects.create(creator=creator, name=kwargs.pop("name", "Settings trip"), **kwargs)
        TripMembership.objects.create(trip=trip, profile=creator, status=TripMembership.STATUS_JOINED, rsvp="yes")
        return trip

    def _patch(self, trip: Trip, body: dict, raw_key: str | None = None):
        """PATCH the settings endpoint for *trip* with *body*.

        Args:
            trip: The trip whose settings to edit.
            body: The JSON body to send.
            raw_key: Optional alternative API key; defaults to the fixture's.

        Returns:
            The test client's response.
        """
        return self.client.patch(
            reverse("external_api:trips.settings", args=[trip.slug]),
            body,
            content_type="application/json",
            **_bearer(raw_key or self.raw_key),
        )


class SetTripPermissionsPresenceTests(_TripSettingsTestCase):
    """``set_trip_permissions`` touches only the fields actually submitted.

    The regression guard for a real data-loss defect: the service used to walk
    a hardcoded ``{field: default}`` table and ``setattr`` *every* entry, so
    any field missing from ``changes`` was reset to that default rather than
    left alone. A one-key partial update therefore rewrote three unrelated
    permissions on a trip shared with other people, with nothing in the
    response to reveal it had happened.
    """

    def _configure(self, trip: Trip) -> None:
        """Set all four permissions to values that differ from the old defaults.

        The old implementation's defaults were ``none`` for
        ``allow_add_members`` and ``everyone`` for the other three, so this
        fixture inverts each one: any field the service silently rewrites is
        then unambiguously visible.

        Args:
            trip: The trip to configure.
        """
        trip.allow_add_members = Trip.PERM_EVERYONE
        trip.allow_add_activities = Trip.PERM_NONE
        trip.allow_edit_activities = Trip.PERM_ORGANIZERS
        trip.allow_comments = Trip.PERM_NONE
        trip.save(update_fields=[*TRIP_PERMISSION_FIELDS, "updated"])

    def test_single_key_change_leaves_the_other_three_untouched(self) -> None:
        """Changing one permission must not silently reset the other three."""
        self._configure(self.trip)

        set_trip_permissions(self.trip, self.profile, changes={"allow_comments": Trip.PERM_ORGANIZERS})

        self.trip.refresh_from_db()
        self.assertEqual(self.trip.allow_comments, Trip.PERM_ORGANIZERS)
        self.assertEqual(self.trip.allow_add_members, Trip.PERM_EVERYONE)
        self.assertEqual(self.trip.allow_add_activities, Trip.PERM_NONE)
        self.assertEqual(self.trip.allow_edit_activities, Trip.PERM_ORGANIZERS)

    def test_empty_changes_is_a_no_op(self) -> None:
        """An empty submission leaves every permission where it was."""
        self._configure(self.trip)

        set_trip_permissions(self.trip, self.profile, changes={})

        self.trip.refresh_from_db()
        self.assertEqual(self.trip.allow_add_members, Trip.PERM_EVERYONE)
        self.assertEqual(self.trip.allow_add_activities, Trip.PERM_NONE)
        self.assertEqual(self.trip.allow_edit_activities, Trip.PERM_ORGANIZERS)
        self.assertEqual(self.trip.allow_comments, Trip.PERM_NONE)

    def test_unknown_level_is_rejected_rather_than_defaulted(self) -> None:
        """A garbage level is a validation failure, not a silent reset.

        Coercing it to the field's default is worse than refusing it: the
        caller is told the write succeeded while the permission moved
        somewhere they never asked for.
        """
        self._configure(self.trip)

        with self.assertRaises(TripValidationError):
            set_trip_permissions(self.trip, self.profile, changes={"allow_comments": "trusted-friends"})

        self.trip.refresh_from_db()
        self.assertEqual(self.trip.allow_comments, Trip.PERM_NONE)

    def test_non_organizer_member_is_refused(self) -> None:
        """Only the creator or an organizer may edit settings."""
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)

        with self.assertRaises(TripPermissionError):
            set_trip_permissions(trip, self.profile, changes={"allow_comments": Trip.PERM_NONE})

    @given(
        submitted=st.dictionaries(st.sampled_from(TRIP_PERMISSION_FIELDS), st.sampled_from(_LEVELS)),
        initial=st.lists(st.sampled_from(_LEVELS), min_size=4, max_size=4),
    )
    def test_only_submitted_fields_ever_move(self, submitted: dict[str, str], initial: list[str]) -> None:
        """For any subset of fields, exactly that subset changes.

        The property the presence-keyed contract actually promises, stated over
        every subset rather than the one the example tests happen to pick:
        a field named in the submission ends up at the submitted level, and a
        field not named ends up exactly where it started.

        Args:
            submitted: An arbitrary subset of the permission fields with levels.
            initial: A starting level for each of the four fields, in
                ``TRIP_PERMISSION_FIELDS`` order.
        """
        trip = self._make_trip(name="Property trip")
        starting = dict(zip(TRIP_PERMISSION_FIELDS, initial, strict=True))
        for field, level in starting.items():
            setattr(trip, field, level)
        trip.save(update_fields=[*TRIP_PERMISSION_FIELDS, "updated"])

        set_trip_permissions(trip, self.profile, changes=submitted)

        trip.refresh_from_db()
        for field in TRIP_PERMISSION_FIELDS:
            self.assertEqual(getattr(trip, field), submitted.get(field, starting[field]))


class TripSettingsEndpointTests(_TripSettingsTestCase):
    """``PATCH /trips/{slug}/settings/`` - the API's only way to edit trip rules."""

    def test_patch_updates_only_the_submitted_level(self) -> None:
        """The endpoint inherits the service's presence-keyed contract."""
        self.trip.allow_add_members = Trip.PERM_EVERYONE
        self.trip.save(update_fields=["allow_add_members", "updated"])

        response = self._patch(self.trip, {"allow_comments": Trip.PERM_ORGANIZERS})

        self.assertEqual(response.status_code, 200)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.allow_comments, Trip.PERM_ORGANIZERS)
        self.assertEqual(self.trip.allow_add_members, Trip.PERM_EVERYONE)

    def test_response_is_the_full_trip_with_recomputed_viewer_flags(self) -> None:
        """One round trip: the write answers with what the client renders from.

        ``viewer.can_*`` is derived server-side from the levels *and* the
        caller's role, so a client that only echoed back the submitted levels
        would still need a second request to know what it may now show.
        """
        response = self._patch(self.trip, {"allow_add_members": Trip.PERM_EVERYONE})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["permissions"]["allow_add_members"], Trip.PERM_EVERYONE)
        self.assertTrue(body["viewer"]["can_add_members"])
        self.assertIn("members", body)
        self.assertIn("calendar_sync", body)

    def test_empty_body_is_400(self) -> None:
        """A submission naming no permission is a client bug, not a no-op success."""
        response = self._patch(self.trip, {})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "Invalid request.")

    def test_unknown_level_is_400(self) -> None:
        """Only the three defined levels are accepted."""
        response = self._patch(self.trip, {"allow_comments": "trusted-friends"})

        self.assertEqual(response.status_code, 400)
        self.assertIn("allow_comments", response.json()["fields"])

    def test_member_who_is_not_an_organizer_gets_403(self) -> None:
        """A deliberate 403: they have already been shown this trip.

        The package's usual answer for an action a caller may not take is 404,
        so that the status cannot confirm the resource exists. That reasoning
        does not apply once the caller is a *member*: they can already read the
        trip in full, so telling them their role is insufficient discloses
        nothing new - and a 404 here would be actively misleading, suggesting
        the trip had vanished.
        """
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED)

        response = self._patch(trip, {"allow_comments": Trip.PERM_NONE})

        self.assertEqual(response.status_code, 403)
        trip.refresh_from_db()
        self.assertEqual(trip.allow_comments, Trip.PERM_EVERYONE)

    def test_organizer_may_edit(self) -> None:
        """A promoted member is an organizer and may change settings."""
        trip = self._make_trip(creator=self.other_profile, name="Theirs")
        TripMembership.objects.create(trip=trip, profile=self.profile, status=TripMembership.STATUS_JOINED, is_organizer=True)

        response = self._patch(trip, {"allow_comments": Trip.PERM_NONE})

        self.assertEqual(response.status_code, 200)
        trip.refresh_from_db()
        self.assertEqual(trip.allow_comments, Trip.PERM_NONE)

    def test_non_member_gets_404_identical_to_a_missing_trip(self) -> None:
        """Someone else's trip must be indistinguishable from one that never existed."""
        theirs = self._make_trip(creator=self.other_profile, name="Private")

        forbidden = self._patch(theirs, {"allow_comments": Trip.PERM_NONE})
        missing = self.client.patch(
            reverse("external_api:trips.settings", args=["no-such-trip-slug"]),
            {"allow_comments": Trip.PERM_NONE},
            content_type="application/json",
            **_bearer(self.raw_key),
        )

        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(forbidden.json(), missing.json())

    def test_read_scope_alone_cannot_write_settings(self) -> None:
        """``trips:read`` must not carry the ability to change a trip's rules."""
        api_key, raw = generate_api_key(self.user, "Read only")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.TRIPS_READ.value])

        response = self._patch(self.trip, {"allow_comments": Trip.PERM_NONE}, raw_key=raw)

        self.assertEqual(response.status_code, 403)

    def test_settings_write_lands_in_the_write_throttle_tier(self) -> None:
        """A settings PATCH is a write, and must be counted as one."""
        from urbanlens.dashboard.external_api.throttling import TIER_WRITE, request_tier
        from urbanlens.dashboard.external_api.views_trips import TripSettingsView

        self.assertEqual(request_tier(TripSettingsView, "PATCH"), TIER_WRITE)
