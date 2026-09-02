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
import re
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripActivityRSVP, TripActivityVote, TripMembership

#: A rendered CSRF token: exactly 64 characters from Django's 62-character
#: alphabet. ``{% csrf_token %}`` re-masks the same secret on every call, so a
#: page embeds several *different* strings (the hidden input, base.html's
#: ``var csrftoken``, the JS config blob) and two renders never match
#: byte-for-byte. Matching on the shape covers all of them at once.
_CSRF_TOKEN_RE = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{64}(?![A-Za-z0-9])")


def _without_csrf_tokens(content: bytes) -> bytes:
    """Blank out per-request CSRF tokens so two responses can be compared.

    The masking is random per render and reveals nothing about the page, so
    normalizing it is what makes an "these two responses are identical" check
    meaningful rather than flaky.

    Args:
        content: A rendered response body.

    Returns:
        The body with every CSRF-token-shaped run replaced by a constant.
    """
    return _CSRF_TOKEN_RE.sub(b"REDACTED", content)


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

    def test_ongoing_multi_day_trip_shows_day_indicator(self):
        today = timezone.now().date()
        trip = _make_trip(self.profile, start_date=today - datetime.timedelta(days=2), end_date=today + datetime.timedelta(days=5))

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertIn("Day 3 of 8", html)
        self.assertIn("trip-card-status--ongoing", html)

    def test_single_day_active_trip_does_not_show_day_indicator(self):
        today = timezone.now().date()
        trip = _make_trip(self.profile, start_date=today, end_date=today)

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertNotIn("trip-card-status--ongoing", html)
        self.assertIn("In progress", html)

    def test_upcoming_multi_day_trip_does_not_show_day_indicator(self):
        today = timezone.now().date()
        trip = _make_trip(self.profile, start_date=today + datetime.timedelta(days=3), end_date=today + datetime.timedelta(days=10))

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertNotIn("trip-card-status--ongoing", html)
        self.assertIn("Upcoming", html)

    def test_rsvp_list_shows_member_chip(self):
        trip = _make_trip(self.profile)  # creator membership defaults to rsvp="yes"

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertIn("trip-card-rsvp-row", html)
        self.assertIn("trip-member-rsvp--yes", html)
        self.assertIn("Going", html)

    def test_pin_count_stat_renders_when_annotated(self):
        trip = _make_trip(self.profile)
        trip.pin_count = 3

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertIn("3 pins", html)

    def test_pin_count_stat_hidden_when_zero(self):
        trip = _make_trip(self.profile)
        trip.pin_count = 0

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertNotIn(" pin", html)

    def test_start_checkin_button_shown_for_joined_members(self):
        trip = _make_trip(self.profile)
        trip.viewer_membership = TripMembership.objects.get(trip=trip, profile=self.profile)

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertIn("Start a check-in", html)
        self.assertIn(f"{reverse('safety.checkin.create')}?trip={trip.slug}", html)

    def test_start_checkin_button_hidden_without_membership(self):
        trip = _make_trip(self.profile)
        # trip.viewer_membership deliberately left unset, as it would be for
        # a viewer whose membership row doesn't exist (shouldn't happen on
        # the real list page, which only shows the viewer's own trips, but
        # covers the template's gating logic in isolation).

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertNotIn("Start a check-in", html)

    def test_open_itinerary_button_always_present(self):
        trip = _make_trip(self.profile)

        html = render_to_string("dashboard/partials/trips/trip_list_partial.html", {"trips": [trip], "profile": self.profile})

        self.assertIn("Open itinerary", html)


class TripListViewTests(TestCase):
    """GET /trips/list/ - end-to-end wiring of pin_count/viewer_membership through the real view."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.client = Client()
        self.client.force_login(self.user)
        self.profile = self.user.profile

    def test_start_checkin_button_appears_for_a_joined_trip(self):
        trip = _make_trip(self.profile)

        response = self.client.get(reverse("trips.list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Start a check-in")
        self.assertContains(response, f"{reverse('safety.checkin.create')}?trip={trip.slug}")

    def test_pin_count_reflects_activity_pins(self):
        trip = _make_trip(self.profile)
        pin = baker.make("dashboard.Pin", profile=self.profile)
        other_pin = baker.make("dashboard.Pin", profile=self.profile)
        TripActivity.objects.create(trip=trip, added_by=self.profile, title="Stop 1", pin=pin)
        TripActivity.objects.create(trip=trip, added_by=self.profile, title="Stop 2", pin=other_pin)
        TripActivity.objects.create(trip=trip, added_by=self.profile, title="Stop 3", pin=pin)
        TripActivity.objects.create(trip=trip, added_by=self.profile, title="No pin")

        response = self.client.get(reverse("trips.list"))

        self.assertContains(response, "2 pins")


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

    def test_post_without_name_generates_a_placeholder(self):
        """A blank name is accepted and gets a generated one (UL-360).

        This test previously asserted a 400, which stopped being true when the
        name became optional so a "just start planning" flow needn't invent a
        title up front - it had been failing ever since. The behavior itself
        now lives in ``services.trips.trip_crud.create_trip``.
        """
        before = set(Trip.objects.values_list("pk", flat=True))
        resp = self.client.post(
            reverse("trips.create"),
            data=json.dumps({"name": ""}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        created = Trip.objects.exclude(pk__in=before).get()
        self.assertTrue(created.name.strip())

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


class CreateTripDialogHxTargetTests(SimpleTestCase):
    """The create-trip dialog's hx-target/hx-swap must match whatever's actually
    on the page it's opened from - see TripCreateViewTests.
    test_post_from_overview_redirects_to_new_trip_via_hx_redirect for the
    backend half of this fix. The overview page has no #trip-list element, so
    targeting it unconditionally made htmx throw htmx:targetError client-side
    (before the request was even sent) for every submission from there."""

    def _render(self, source: str) -> str:
        return render_to_string("dashboard/partials/trips/_create_trip_dialog.html", {"source": source})

    def test_overview_source_does_not_target_trip_list(self) -> None:
        html = self._render("overview")
        self.assertNotIn('hx-target="#trip-list"', html)

    def test_overview_source_targets_the_form_itself(self) -> None:
        html = self._render("overview")
        self.assertIn('hx-target="this"', html)
        self.assertIn('hx-swap="none"', html)

    def test_list_source_still_targets_trip_list(self) -> None:
        html = self._render("list")
        self.assertIn('hx-target="#trip-list"', html)
        self.assertIn('hx-swap="innerHTML"', html)

    def test_default_source_behaves_like_list(self) -> None:
        """No source given (e.g. a stale include) should keep the original,
        long-standing #trip-list behavior rather than the newer overview one."""
        html = render_to_string("dashboard/partials/trips/_create_trip_dialog.html", {})
        self.assertIn('hx-target="#trip-list"', html)


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

    def test_outsider_gets_404_indistinguishable_from_a_missing_trip(self):
        """Someone else's trip must look exactly like one that doesn't exist.

        This used to be a 403 while a missing slug was a 404, so the status
        code alone let anyone enumerate valid private trip slugs - despite both
        rendering the same "not found" page specifically to prevent that. See
        ``services.trips.trip_access.get_trip_for_viewer``.
        """
        client = Client()
        client.force_login(self.outsider_user)
        forbidden = client.get(self._url())
        missing = client.get(reverse("trips.detail", kwargs={"trip_slug": "no-such-trip-slug"}))
        self.assertEqual(forbidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        # Django masks the CSRF token afresh per render, so the values differ
        # between any two responses - including two identical ones. They leak
        # nothing about the trip; everything else must match exactly.
        self.assertEqual(_without_csrf_tokens(forbidden.content), _without_csrf_tokens(missing.content))

    def test_nonexistent_trip_returns_404(self):
        client = Client()
        client.force_login(self.creator_user)
        url = reverse("trips.detail", kwargs={"trip_slug": "no-such-trip-slug"})
        resp = client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_map_default_layer_matches_the_profiles_main_map_setting(self):
        """The trip map used to always start on window.MapLayers.create()'s own
        hardcoded default, ignoring the user's actual default_map_view/
        map_dark_mode settings entirely - now mirrors the main map's own
        defaultBase/darkMode/storageKey wiring exactly (map/index.html)."""
        self.creator.default_map_view = "topographic"
        self.creator.map_dark_mode = "dark"
        self.creator.save(update_fields=["default_map_view", "map_dark_mode"])
        client = Client()
        client.force_login(self.creator_user)

        resp = client.get(self._url())

        self.assertEqual(resp.context["default_map_view"], "topographic")
        self.assertEqual(resp.context["map_dark_mode"], "dark")
        content = resp.content.decode()
        self.assertIn("defaultBase: 'topographic'", content)
        self.assertIn("darkMode: 'dark'", content)
        self.assertIn(f"ul_layers_v1_{self.creator.uuid}", content)

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

    def test_activity_attribution_shows_full_name_not_username(self):
        """Regression guard: the "Added by" line used to show the raw
        username even when the adder has a real name set."""
        self.member_user.first_name = "Pat"
        self.member_user.last_name = "Rivera"
        self.member_user.save(update_fields=["first_name", "last_name"])
        TripActivity.objects.create(trip=self.trip, title="Explore the mill", added_by=self.member)

        client = Client()
        client.force_login(self.creator_user)
        resp = client.get(self._url())

        self.assertContains(resp, "Pat Rivera")
        self.assertNotContains(resp, self.member_user.username)

    def test_scheduled_date_only_produces_a_timezone_aware_datetime(self):
        """Regression guard: _parse_scheduled_at used to build a naive
        datetime.combine(date, midnight) with no tzinfo, tripping Django's
        "received a naive datetime while time zone support is active"
        RuntimeWarning on every date-only activity (repeating in production
        logs) and silently storing the wrong calendar day for any user not
        in the server's own timezone."""
        client = Client()
        client.force_login(self.creator_user)
        resp = client.post(
            self._url(),
            data=json.dumps({"title": "Explore Site", "scheduled_date": "2026-08-10"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        activity = TripActivity.objects.get(trip=self.trip, title="Explore Site")
        self.assertIsNotNone(activity.scheduled_at)
        self.assertTrue(timezone.is_aware(activity.scheduled_at))

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

    def test_outsider_gets_404(self):
        """A non-member is told the trip does not exist, not that it's forbidden.

        See TripDetailViewTests.test_outsider_gets_404_indistinguishable_from_a_missing_trip
        for why the old 403 was an enumeration leak.
        """
        outsider = baker.make("auth.User")
        client = Client()
        client.force_login(outsider)
        resp = client.get(self._url())
        self.assertEqual(resp.status_code, 404)

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

    @override_settings(TIME_ZONE="Pacific/Kiritimati")
    def test_completion_clamps_against_the_configured_timezone_not_the_server_clock(self):
        """"Today" must mean today in Django's TIME_ZONE, not the host OS's date.

        At this fixed instant the configured zone (UTC+14) is already on Jan 2
        while UTC is still on Jan 1, so a `date.today()`-based clamp would reject
        a legitimately "today" completion as a future date and cap it a day early.
        """
        instant = datetime.datetime(2026, 1, 1, 20, 0, tzinfo=datetime.UTC)
        local_today = timezone.localtime(instant).date()
        self.assertEqual(local_today, datetime.date(2026, 1, 2))

        with patch("django.utils.timezone.now", return_value=instant):
            self.client.post(self._url(), data={"completed_date": local_today.isoformat()})

        self.activity.refresh_from_db()
        self.assertEqual(self.activity.status, TripActivity.STATUS_COMPLETED)
        self.assertEqual(timezone.localtime(self.activity.scheduled_at).date(), local_today)

    def test_completed_date_produces_a_timezone_aware_scheduled_at(self):
        """Regression guard for the same naive-datetime bug as
        test_scheduled_date_only_produces_a_timezone_aware_datetime, but on
        the "mark completed" path's own datetime.combine call."""
        self.client.post(self._url(), data={"completed_date": "2025-06-01"})
        self.activity.refresh_from_db()
        self.assertIsNotNone(self.activity.scheduled_at)
        self.assertTrue(timezone.is_aware(self.activity.scheduled_at))

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

    def test_cannot_add_a_user_who_blocked_the_inviter(self) -> None:
        """A block must stop this the same way it stops a direct message -
        forcing a membership row + notification onto someone is exactly the
        unsolicited contact a block exists to prevent."""
        blocker = baker.make("auth.User", username="has-blocked-creator").profile
        Friendship.objects.create(from_profile=blocker, to_profile=self.creator, status=FriendshipStatus.BLOCKED)

        resp = self.client.post(
            self._url(),
            data=json.dumps({"username": "has-blocked-creator"}),
            content_type="application/json",
        )

        # 404, identical to an unknown username - a 403 here would confirm the
        # account exists and is blocking the caller, which is itself an
        # enumeration leak.
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No user found", resp.content.decode())
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=blocker).exists())

    def test_cannot_add_a_user_the_inviter_blocked(self) -> None:
        """Blocking is mutual - it must not matter which side initiated it."""
        target = baker.make("auth.User", username="blocked-by-creator").profile
        Friendship.objects.create(from_profile=self.creator, to_profile=target, status=FriendshipStatus.BLOCKED)

        resp = self.client.post(
            self._url(),
            data=json.dumps({"username": "blocked-by-creator"}),
            content_type="application/json",
        )

        # Same as the reverse-direction block above: 404, not 403.
        self.assertEqual(resp.status_code, 404)
        self.assertIn("No user found", resp.content.decode())
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=target).exists())


class TripAddableFriendsPickerTests(TestCase):
    """The add-member dialog's friend picker (trip_members_panel.html/_addable_friends).

    Regression coverage: the dialog used to be a bare "type the exact
    username" box with no way to browse the creator's friends - the picker
    lives in trip_members_panel.html (not detail.html) specifically so it
    stays in sync after an add/remove, rather than showing a stale list.
    """

    def setUp(self):
        super().setUp()
        self.creator_user = baker.make("auth.User")
        self.creator = self.creator_user.profile
        self.trip = _make_trip(self.creator)
        self.client = Client()
        self.client.force_login(self.creator_user)

    def _befriend(self, username: str) -> Profile:
        friend = baker.make("auth.User", username=username).profile
        Friendship.objects.create(from_profile=self.creator, to_profile=friend, status=FriendshipStatus.ACCEPTED)
        return friend

    def test_creator_sees_friends_not_already_on_the_trip(self):
        friend = self._befriend("addable-friend")

        resp = self.client.get(reverse("trips.members", kwargs={"trip_slug": self.trip.slug}))

        self.assertContains(resp, friend.user.username)
        self.assertContains(resp, "trip-add-friend-btn")

    def test_friend_already_on_trip_is_excluded(self):
        """Regression guard: this used to assert the username never appears
        anywhere in the response at all, which false-failed the moment the
        member list itself (a different section of the same page) started
        legitimately rendering it - being a real trip member is exactly what
        "already on the trip" means. What actually must exclude them is the
        add-member dialog's friend picker specifically, checked here via its
        hidden username input (see the picker's own markup)."""
        friend = self._befriend("already-in")
        TripMembership.objects.create(trip=self.trip, profile=friend)

        resp = self.client.get(reverse("trips.members", kwargs={"trip_slug": self.trip.slug}))

        self.assertContains(resp, "@already-in")  # still a real member, shown in the list
        self.assertNotContains(resp, 'value="already-in"')  # but not offered again in the picker

    def test_non_creator_sees_no_picker(self):
        friend = self._befriend("visible-to-creator-only")
        TripMembership.objects.create(trip=self.trip, profile=friend)
        other_member = baker.make("auth.User", username="plain-member").profile
        TripMembership.objects.create(trip=self.trip, profile=other_member)
        self.client.force_login(other_member.user)

        resp = self.client.get(reverse("trips.members", kwargs={"trip_slug": self.trip.slug}))

        self.assertNotContains(resp, "trip-add-friend-btn")

    def test_picking_a_friend_adds_them_via_the_same_endpoint(self):
        friend = self._befriend("click-to-add")

        resp = self.client.post(
            reverse("trips.members", kwargs={"trip_slug": self.trip.slug}),
            data=json.dumps({"username": friend.user.username}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(TripMembership.objects.filter(trip=self.trip, profile=friend).exists())


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


class TripActivityRSVPViewTests(TestCase):
    """POST an activity RSVP override and fall back to the trip RSVP by default."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = _make_trip(self.profile)
        self.activity = TripActivity.objects.create(trip=self.trip, added_by=self.profile, title="First stop")
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "trips.activity.rsvp",
            kwargs={"trip_slug": self.trip.slug, "activity_id": self.activity.id},
        )

    def test_activity_inherits_trip_rsvp_without_an_override(self):
        self.assertEqual(TripActivityRSVP.effective_for(self.activity, self.profile), TripMembership.RSVP_YES)

    def test_set_activity_override(self):
        response = self.client.post(self._url(), {"rsvp": "no"})

        self.assertEqual(response.status_code, 200)
        override = TripActivityRSVP.objects.get(activity=self.activity, membership__profile=self.profile)
        self.assertEqual(override.rsvp, TripMembership.RSVP_NO)
        self.assertEqual(TripActivityRSVP.effective_for(self.activity, self.profile), TripMembership.RSVP_NO)
        self.assertContains(response, "Not coming")
        self.assertContains(response, "Overrides trip RSVP")

    def test_clear_activity_override_restores_inheritance(self):
        TripActivityRSVP.objects.create(
            activity=self.activity,
            membership=TripMembership.objects.get(trip=self.trip, profile=self.profile),
            rsvp=TripMembership.RSVP_NO,
        )

        response = self.client.post(self._url(), {"rsvp": ""})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripActivityRSVP.objects.filter(activity=self.activity, membership__profile=self.profile).exists())
        self.assertEqual(TripActivityRSVP.effective_for(self.activity, self.profile), TripMembership.RSVP_YES)
        self.assertContains(response, "From trip RSVP")

    def test_trip_rsvp_changes_inherited_activities_but_preserves_overrides(self):
        other_activity = TripActivity.objects.create(trip=self.trip, added_by=self.profile, title="Second stop")
        TripActivityRSVP.objects.create(
            activity=self.activity,
            membership=TripMembership.objects.get(trip=self.trip, profile=self.profile),
            rsvp=TripMembership.RSVP_NO,
        )

        response = self.client.post(
            reverse("trips.rsvp", kwargs={"trip_slug": self.trip.slug}),
            {"rsvp": "maybe"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TripActivityRSVP.effective_for(self.activity, self.profile), TripMembership.RSVP_NO)
        self.assertEqual(TripActivityRSVP.effective_for(other_activity, self.profile), TripMembership.RSVP_MAYBE)
        self.assertContains(response, 'id="trip-activities-panel"')

    def test_invalid_activity_rsvp_is_rejected(self):
        response = self.client.post(self._url(), {"rsvp": "absolutely"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(TripActivityRSVP.objects.filter(activity=self.activity, membership__profile=self.profile).exists())


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
        activity = TripActivity.objects.create(trip=self.trip, added_by=self.creator, title="Stop")
        membership = TripMembership.objects.get(trip=self.trip, profile=self.member)
        TripActivityRSVP.objects.create(activity=activity, membership=membership, rsvp=TripMembership.RSVP_YES)
        client = Client()
        client.force_login(self.member_user)
        resp = client.delete(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            TripMembership.objects.filter(trip=self.trip, profile=self.member).exists(),
        )
        self.assertFalse(TripActivityRSVP.objects.filter(activity=activity).exists())

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


class TripActivityMoveViewTests(TestCase):
    """POST /trips/<slug>/activities/<id>/move/ - drag-to-reschedule."""

    def setUp(self):
        super().setUp()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.trip = _make_trip(self.profile)
        self.activity = TripActivity.objects.create(
            trip=self.trip,
            added_by=self.profile,
            title="Reschedule Me",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "trips.activity.move",
            kwargs={"trip_slug": self.trip.slug, "activity_id": self.activity.id},
        )

    def test_rescheduling_produces_a_timezone_aware_datetime_with_no_prior_time(self):
        """Regression guard for the same naive-datetime bug as the other
        scheduled_at-construction tests, but on the drag-to-reschedule path's
        "no existing scheduled_at" branch."""
        resp = self.client.post(
            self._url(),
            data=json.dumps({"date": "2026-08-10"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.activity.refresh_from_db()
        self.assertIsNotNone(self.activity.scheduled_at)
        self.assertTrue(timezone.is_aware(self.activity.scheduled_at))

    def test_rescheduling_preserves_existing_time_and_stays_aware(self):
        self.activity.scheduled_at = timezone.make_aware(datetime.datetime(2026, 8, 1, 14, 30))
        self.activity.save(update_fields=["scheduled_at"])

        resp = self.client.post(
            self._url(),
            data=json.dumps({"date": "2026-08-10"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.activity.refresh_from_db()
        self.assertTrue(timezone.is_aware(self.activity.scheduled_at))
        self.assertEqual(self.activity.scheduled_at.date(), datetime.date(2026, 8, 10))
