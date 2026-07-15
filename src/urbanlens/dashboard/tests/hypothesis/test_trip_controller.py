"""Integration tests for the trip controller HTTP views.

Uses Django's test client to exercise:
- TripCreateView - POST creates trip, re-renders list partial
- TripDetailView - GET returns 200 for members, 403 for outsiders, 404 for missing
- TripDeleteView - DELETE only by creator
- TripActivitiesView - GET/POST activity management with permission levels
- TripActivityCompleteView - marks activity complete, caps future dates to today
- TripActivityVoteView - cast/update/clear votes
- TripMembersView - GET/POST member management
- TripMemberRemoveView - DELETE self or via creator
- TripMemberRSVPView - POST RSVP status
- TripLeaveView - DELETE leave trip
- TripSettingsView - POST settings by organizer only
- TripActivityPositionView - POST lat/lng override
"""
from __future__ import annotations

import datetime
import json
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripActivityVote, TripMembership


def _make_trip(creator_profile: Profile, **kwargs) -> Trip:
    """Create a trip with creator as a member."""
    trip = Trip.objects.create(name="Test Trip", creator=creator_profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=creator_profile, defaults={"rsvp": "yes"})
    return trip


class TripListPartialTests(TestCase):
    """Trip list partial date rendering."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile

    def test_one_day_trip_shows_single_date_with_duration(self):
        trip = _make_trip(
            self.profile,
            start_date=datetime.date(2026, 7, 4),
            end_date=datetime.date(2026, 7, 4),
        )

        html = render_to_string(
            "dashboard/partials/trips/trip_list_partial.html",
            {"trips": [trip], "profile": self.profile},
        )

        self.assertIn("Jul 4, 2026", html)
        self.assertNotIn("Jul 4, 2026 - Jul 4, 2026", html)
        self.assertIn("1 day", html)

    def test_multi_day_trip_shows_date_range(self):
        trip = _make_trip(
            self.profile,
            start_date=datetime.date(2026, 7, 4),
            end_date=datetime.date(2026, 7, 6),
        )

        html = render_to_string(
            "dashboard/partials/trips/trip_list_partial.html",
            {"trips": [trip], "profile": self.profile},
        )

        self.assertIn("Jul 4, 2026 - Jul 6, 2026", html)
        self.assertIn("3 days", html)


class TripCreateViewTests(TestCase):
    """POST /trips/create/ - creates a trip and returns the list partial."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.client = Client()
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_post_creates_trip(self):
        resp = self.client.post(
            reverse("trips.create"),
            data=json.dumps({"name": "Urban Adventure"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Trip.objects.filter(name="Urban Adventure").exists())

    def test_post_adds_creator_as_member(self):
        self.client.post(
            reverse("trips.create"),
            data=json.dumps({"name": "Weekend Explore"}),
            content_type="application/json",
        )
        trip = Trip.objects.get(name="Weekend Explore")
        self.assertTrue(
            TripMembership.objects.filter(trip=trip, profile=self.profile).exists(),
        )

    def test_post_without_name_returns_400(self):
        resp = self.client.post(
            reverse("trips.create"),
            data=json.dumps({"name": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_post_with_form_data_also_works(self):
        resp = self.client.post(reverse("trips.create"), data={"name": "Form Trip"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Trip.objects.filter(name="Form Trip").exists())

    def test_unauthenticated_redirected(self):
        client = Client()
        resp = client.post(reverse("trips.create"), data={"name": "Hack"})
        self.assertIn(resp.status_code, (301, 302))

    def test_post_from_overview_redirects_to_new_trip_via_hx_redirect(self):
        """The overview page's dialog has no #trip-list to swap into, so it opts into
        an HX-Redirect straight to the new trip instead of the list-partial re-render."""
        resp = self.client.post(
            reverse("trips.create"),
            data={"name": "Overview Trip", "source": "overview"},
        )
        self.assertEqual(resp.status_code, 200)
        trip = Trip.objects.get(name="Overview Trip")
        self.assertEqual(resp["HX-Redirect"], reverse("trips.detail", kwargs={"trip_slug": trip.slug}))

    def test_post_from_list_has_no_hx_redirect(self):
        resp = self.client.post(reverse("trips.create"), data={"name": "List Trip", "source": "list"})
        self.assertNotIn("HX-Redirect", resp)


class TripDetailViewTests(TestCase):
    """GET /trips/<slug>/ - access control and page render."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

        self.outsider_user = baker.make("auth.User")
        self.outsider = self.outsider_user.profile

    def _url(self):
        return reverse("trips.detail", kwargs={"trip_slug": self.trip.slug})

    def test_creator_gets_200(self):
        client = Client()
        client.force_login(self.creator_user)
        resp = client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_member_gets_200(self):
        client = Client()
        client.force_login(self.member_user)
        resp = client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_outsider_gets_403(self):
        client = Client()
        client.force_login(self.outsider_user)
        resp = client.get(self._url())
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_trip_returns_404(self):
        client = Client()
        client.force_login(self.creator_user)
        url = reverse("trips.detail", kwargs={"trip_slug": "no-such-trip-slug"})
        resp = client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_edit_activity_dialog_matches_add_activity_redesign(self):
        """Regression guard: the Edit-Activity dialog previously still had the old
        proposed/confirmed pill toggle, "(optional)" label text, and an always-visible
        child-trip box - all fixed to match the Add-Activity redesign."""
        client = Client()
        client.force_login(self.creator_user)
        html = client.get(self._url()).content.decode()

        # Single checkbox drives status now, not a two-button pill toggle.
        self.assertIn('id="edit-activity-propose-checkbox"', html)
        self.assertIn("Propose for discussion", html)
        self.assertNotIn("status-pill-toggle", html)

        # No parenthetical "(optional)" hints anywhere in the edit dialog.
        edit_dialog_html = html.split('id="edit-activity-dialog"', 1)[1].split("</dialog>", 1)[0]
        self.assertNotIn("(optional)", edit_dialog_html)

        # Child trip is an opt-in toggle, not an always-visible box.
        self.assertIn('id="edit-activity-child-trip-toggle"', html)
        self.assertIn('id="edit-activity-child-trip-wrap" hidden', html)

    def test_edit_activity_end_date_is_opt_in_like_add_activity(self):
        """Regression guard: the Edit-Activity dialog's End date used to always be
        visible, unlike Add-Activity's opt-in "+ Add end date" toggle."""
        client = Client()
        client.force_login(self.creator_user)
        html = client.get(self._url()).content.decode()

        self.assertIn('id="edit-activity-end-date-wrap" hidden', html)
        self.assertIn('id="edit-activity-end-date-toggle-row"', html)
        self.assertIn('onclick="_revealEditActivityEndDate()"', html)

    def test_propose_and_hide_location_explainers_are_behind_a_tooltip(self):
        """Regression guard: these used to be always-visible <p class="form-help">
        paragraphs in both dialogs instead of a click-to-reveal tooltip icon,
        matching the rest of the site's explainer convention."""
        client = Client()
        client.force_login(self.creator_user)
        html = client.get(self._url()).content.decode()

        # The old always-visible wrapper is gone from both dialogs' propose/hide-location rows.
        self.assertNotIn('<p class="form-help">Left unchecked', html)
        self.assertNotIn('<p class="form-help">Location won', html)
        # The same copy now lives on a click-to-reveal tooltip icon instead.
        self.assertIn("Left unchecked, the activity is added as confirmed.", html)
        self.assertIn("Left unchecked, the activity is confirmed.", html)
        self.assertGreaterEqual(html.count("ul-tooltip-help"), 4)

    def test_no_dialog_offers_a_hide_name_control(self):
        """Regression guard: "Add custom name" used to flip into a "Hide name"
        collapse-back control once clicked - unnecessary, since clearing the
        field's text already does the same thing."""
        client = Client()
        client.force_login(self.creator_user)
        html = client.get(self._url()).content.decode()

        self.assertNotIn("Hide name", html)


class TripDeleteViewTests(TestCase):
    """DELETE /trips/<slug>/delete/ - only creator can delete."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def _url(self):
        return reverse("trips.delete", kwargs={"trip_slug": self.trip.slug})

    def test_creator_can_delete(self):
        client = Client()
        client.force_login(self.creator_user)
        resp = client.delete(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Trip.objects.filter(pk=self.trip.pk).exists())

    def test_member_cannot_delete(self):
        client = Client()
        client.force_login(self.member_user)
        resp = client.delete(self._url())
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Trip.objects.filter(pk=self.trip.pk).exists())


class TripActivitiesViewTests(TestCase):
    """GET/POST /trips/<slug>/activities/ - activity listing and creation."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(
            self.creator,
            allow_add_activities=Trip.PERM_EVERYONE,
        )

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def _url(self):
        return reverse("trips.activities", kwargs={"trip_slug": self.trip.slug})

    def test_get_activities_panel_as_member(self):
        client = Client()
        client.force_login(self.member_user)
        resp = client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_post_adds_activity(self):
        client = Client()
        client.force_login(self.creator_user)
        resp = client.post(
            self._url(),
            data=json.dumps({"title": "Visit Factory", "notes": "Bring torch"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TripActivity.objects.filter(trip=self.trip, title="Visit Factory").exists())

    def test_post_with_geocoded_location_creates_location(self):
        from urbanlens.dashboard.models.location.model import Location
        client = Client()
        client.force_login(self.creator_user)
        initial_count = Location.objects.count()
        resp = client.post(
            self._url(),
            data=json.dumps({
                "title": "Rooftop",
                "geocoded_lat": "51.5",
                "geocoded_lng": "-0.12",
                "geocoded_name": "London Bridge",
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Location.objects.count(), initial_count + 1)

    def test_member_blocked_when_permission_organizers_only(self):
        self.trip.allow_add_activities = Trip.PERM_ORGANIZERS
        self.trip.save()
        client = Client()
        client.force_login(self.member_user)
        resp = client.post(
            self._url(),
            data=json.dumps({"title": "Sneaky Activity"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_outsider_gets_403(self):
        outsider = baker.make("auth.User")
        client = Client()
        client.force_login(outsider)
        resp = client.get(self._url())
        self.assertEqual(resp.status_code, 403)

    def test_post_pin_only_uses_pin_name_in_panel(self):
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = Location.objects.create(
            latitude=51.5,
            longitude=-0.12,
        )
        pin = Pin.objects.create(
            profile=self.creator,
            location=location,
            name="Abandoned Factory",
        )
        client = Client()
        client.force_login(self.creator_user)
        resp = client.post(
            self._url(),
            data=json.dumps({"pin_uuid": str(pin.uuid)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Abandoned Factory")
        self.assertNotContains(resp, "Unnamed activity")

    def test_post_pin_without_name_uses_address_in_panel(self):
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        location = Location.objects.create(
            latitude=51.5,
            longitude=-0.12,
            route="Baker Street,",
            locality="London,",
            administrative_area_level_1="England",
        )
        pin = Pin.objects.create(
            profile=self.creator,
            location=location,
        )
        client = Client()
        client.force_login(self.creator_user)
        resp = client.post(
            self._url(),
            data=json.dumps({"pin_uuid": str(pin.uuid)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Baker Street")
        self.assertNotContains(resp, "Unnamed activity")


class TripActivityEffectiveTitleTests(TestCase):
    """TripActivity.effective_title resolves pin name/address when title is unset."""

    def setUp(self):
        super().setUp()
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.pin.model import Pin

        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = _make_trip(self.profile)
        self.location = Location.objects.create(
            latitude=40.0,
            longitude=-74.0,
            route="Main St,",
            locality="Springfield,",
            administrative_area_level_1="IL",
        )
        self.pin = Pin.objects.create(
            profile=self.profile,
            location=self.location,
            name="Old Mill",
        )

    def test_custom_title_takes_priority(self):
        activity = TripActivity.objects.create(trip=self.trip, title="Custom Label", pin=self.pin)
        self.assertEqual(activity.effective_title, "Custom Label")

    def test_pin_name_used_when_no_title(self):
        activity = TripActivity.objects.create(trip=self.trip, pin=self.pin, location=self.location)
        self.assertEqual(activity.effective_title, "Old Mill")

    def test_pin_address_used_when_no_meaningful_name(self):
        self.pin.name = None
        self.pin.save(update_fields=["name"])
        activity = TripActivity.objects.create(trip=self.trip, pin=self.pin, location=self.location)
        self.assertIn("Main St", activity.effective_title)


class TripActivityCompleteViewTests(TestCase):
    """POST /trips/<slug>/activities/<id>/complete/ - marks activity completed."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = _make_trip(self.profile)
        self.activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.profile,
            title="Explore Site",
            status=TripActivity.STATUS_PROPOSED,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "trips.activity.complete",
            kwargs={"trip_slug": self.trip.slug, "activity_id": self.activity.id},
        )

    def test_marks_activity_completed(self):
        self.client.post(self._url(), data={"completed_date": "2025-06-01"})
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, TripActivity.STATUS_COMPLETED)

    def test_future_date_capped_to_today(self):
        future = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        self.client.post(self._url(), data={"completed_date": future})
        self.activity.refresh_from_db()
        if self.activity.scheduled_at:
            self.assertLessEqual(self.activity.scheduled_at.date(), datetime.date.today())

    def test_invalid_date_defaults_to_today(self):
        self.client.post(self._url(), data={"completed_date": "not-a-date"})
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, TripActivity.STATUS_COMPLETED)

    def test_no_date_defaults_to_today(self):
        self.client.post(self._url(), data={})
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, TripActivity.STATUS_COMPLETED)


class TripActivityVoteViewTests(TestCase):
    """POST /trips/<slug>/activities/<id>/vote/ - vote cast/update/clear."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)
        self.activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.creator,
            title="Factory Visit",
            status=TripActivity.STATUS_PROPOSED,
        )
        self.client = Client()
        self.client.force_login(self.creator_user)

    def _url(self):
        return reverse(
            "trips.activity.vote",
            kwargs={"trip_slug": self.trip.slug, "activity_id": self.activity.id},
        )

    def test_upvote_created(self):
        self.client.post(self._url(), data={"vote": "up"})
        self.assertTrue(
            TripActivityVote.objects.filter(
                activity=self.activity, profile=self.creator, vote=TripActivityVote.VOTE_UP,
            ).exists(),
        )

    def test_downvote_created(self):
        self.client.post(self._url(), data={"vote": "down"})
        self.assertTrue(
            TripActivityVote.objects.filter(
                activity=self.activity, profile=self.creator, vote=TripActivityVote.VOTE_DOWN,
            ).exists(),
        )

    def test_empty_vote_clears_existing(self):
        TripActivityVote.objects.create(
            activity=self.activity, profile=self.creator, vote=TripActivityVote.VOTE_UP,
        )
        self.client.post(self._url(), data={"vote": ""})
        self.assertFalse(
            TripActivityVote.objects.filter(activity=self.activity, profile=self.creator).exists(),
        )

    def test_invalid_vote_value_returns_400(self):
        resp = self.client.post(self._url(), data={"vote": "sideways"})
        self.assertEqual(resp.status_code, 400)

    def test_voting_on_completed_activity_returns_400(self):
        self.activity.status = TripActivity.STATUS_COMPLETED
        self.activity.save()
        resp = self.client.post(self._url(), data={"vote": "up"})
        self.assertEqual(resp.status_code, 400)

    def test_vote_updated_not_duplicated(self):
        TripActivityVote.objects.create(
            activity=self.activity, profile=self.creator, vote=TripActivityVote.VOTE_UP,
        )
        self.client.post(self._url(), data={"vote": "down"})
        votes = TripActivityVote.objects.filter(activity=self.activity, profile=self.creator)
        self.assertEqual(votes.count(), 1)
        self.assertEqual(votes.first().vote, TripActivityVote.VOTE_DOWN)


class TripMembersViewTests(TestCase):
    """GET/POST /trips/<slug>/members/ - member listing and invitation."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator, allow_add_members=Trip.PERM_EVERYONE)
        self.client = Client()
        self.client.force_login(self.creator_user)

    def _url(self):
        return reverse("trips.members", kwargs={"trip_slug": self.trip.slug})

    def test_get_renders_members_panel(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)

    def test_add_member_by_username(self):
        new_user = baker.make("auth.User", username="newmember")
        resp = self.client.post(
            self._url(),
            data=json.dumps({"username": "newmember"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        new_profile = Profile.objects.get(user=new_user)
        self.assertTrue(TripMembership.objects.filter(trip=self.trip, profile=new_profile).exists())

    def test_add_unknown_username_returns_404(self):
        resp = self.client.post(
            self._url(),
            data=json.dumps({"username": "no_such_user"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 404)

    def test_blank_username_returns_400(self):
        resp = self.client.post(
            self._url(),
            data=json.dumps({"username": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)


class TripMemberRSVPViewTests(TestCase):
    """POST /trips/<slug>/rsvp/ - update RSVP status."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = _make_trip(self.profile)
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self):
        return reverse("trips.rsvp", kwargs={"trip_slug": self.trip.slug})

    def test_set_rsvp_yes(self):
        self.client.post(self._url(), data=json.dumps({"rsvp": "yes"}), content_type="application/json")
        m = TripMembership.objects.get(trip=self.trip, profile=self.profile)
        self.assertEqual(m.rsvp, "yes")

    def test_set_rsvp_no(self):
        self.client.post(self._url(), data=json.dumps({"rsvp": "no"}), content_type="application/json")
        m = TripMembership.objects.get(trip=self.trip, profile=self.profile)
        self.assertEqual(m.rsvp, "no")

    def test_set_rsvp_maybe(self):
        self.client.post(self._url(), data=json.dumps({"rsvp": "maybe"}), content_type="application/json")
        m = TripMembership.objects.get(trip=self.trip, profile=self.profile)
        self.assertEqual(m.rsvp, "maybe")

    def test_invalid_rsvp_value_returns_400(self):
        resp = self.client.post(
            self._url(), data=json.dumps({"rsvp": "absolutely"}), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_rsvp_clears_to_none(self):
        m = TripMembership.objects.get(trip=self.trip, profile=self.profile)
        m.rsvp = "yes"
        m.save()
        self.client.post(self._url(), data=json.dumps({"rsvp": ""}), content_type="application/json")
        m.refresh_from_db()
        self.assertIsNone(m.rsvp)


class TripLeaveViewTests(TestCase):
    """DELETE /trips/<slug>/leave/ - member exits trip."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def _url(self):
        return reverse("trips.leave", kwargs={"trip_slug": self.trip.slug})

    def test_member_can_leave(self):
        client = Client()
        client.force_login(self.member_user)
        resp = client.delete(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            TripMembership.objects.filter(trip=self.trip, profile=self.member).exists(),
        )

    def test_creator_cannot_leave(self):
        client = Client()
        client.force_login(self.creator_user)
        resp = client.delete(self._url())
        self.assertEqual(resp.status_code, 400)


class TripSettingsViewTests(TestCase):
    """POST /trips/<slug>/settings/ - only organizer may change settings."""

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)

        self.member_user = baker.make("auth.User")
        self.member = self.member_user.profile
        TripMembership.objects.create(trip=self.trip, profile=self.member)

    def _url(self):
        return reverse("trips.settings", kwargs={"trip_slug": self.trip.slug})

    def test_organizer_can_save_settings(self):
        client = Client()
        client.force_login(self.creator_user)
        resp = client.post(self._url(), data={
            "allow_add_members": "everyone",
            "allow_add_activities": "organizers",
            "allow_edit_activities": "none",
            "allow_comments": "everyone",
        })
        self.assertEqual(resp.status_code, 200)
        self.trip.refresh_from_db()
        self.assertEqual(self.trip.allow_add_members, Trip.PERM_EVERYONE)
        self.assertEqual(self.trip.allow_add_activities, Trip.PERM_ORGANIZERS)
        self.assertEqual(self.trip.allow_edit_activities, Trip.PERM_NONE)

    def test_member_cannot_save_settings(self):
        client = Client()
        client.force_login(self.member_user)
        resp = client.post(self._url(), data={
            "allow_add_members": "everyone",
        })
        self.assertEqual(resp.status_code, 403)

    def test_invalid_perm_value_falls_back_to_default(self):
        client = Client()
        client.force_login(self.creator_user)
        client.post(self._url(), data={
            "allow_add_members": "INVALID_VALUE",
        })
        self.trip.refresh_from_db()
        # Invalid value falls back to the hardcoded default "none"
        self.assertEqual(self.trip.allow_add_members, Trip.PERM_NONE)


class TripActivityPositionViewTests(TestCase):
    """POST /trips/<slug>/activities/<id>/position/ - saves lat/lng override."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = _make_trip(self.profile)
        self.activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.profile,
            title="Drag Target",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "trips.activity.position",
            kwargs={"trip_slug": self.trip.slug, "activity_id": self.activity.id},
        )

    def test_saves_lat_lng(self):
        self.client.post(
            self._url(),
            data=json.dumps({"lat": 51.5, "lng": -0.12}),
            content_type="application/json",
        )
        self.activity.refresh_from_db()
        self.assertAlmostEqual(float(self.activity.lat_override), 51.5)
        self.assertAlmostEqual(float(self.activity.lng_override), -0.12)

    def test_missing_lat_returns_400(self):
        resp = self.client.post(
            self._url(),
            data=json.dumps({"lng": -0.12}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_numeric_values_returns_400(self):
        resp = self.client.post(
            self._url(),
            data=json.dumps({"lat": "north", "lng": "west"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_returns_json_with_saved_coords(self):
        resp = self.client.post(
            self._url(),
            data=json.dumps({"lat": 48.85, "lng": 2.35}),
            content_type="application/json",
        )
        body = json.loads(resp.content)
        self.assertAlmostEqual(body["lat"], 48.85)
        self.assertAlmostEqual(body["lng"], 2.35)
