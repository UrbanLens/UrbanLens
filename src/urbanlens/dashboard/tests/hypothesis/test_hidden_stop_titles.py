"""A hidden stop never shows a member who may not see it a name taken from its place.

A stop picked from a place search with no title typed stores the place's name as its title (P186), and an imported
calendar event's location becomes an activity's title. ``TripActivity.title_from_place`` records that, and every
surface that masks a hidden stop masks such a title with its location: the activities panel (its text and its data
attributes), the external API (``title`` and ``effective_title``), the Google Calendar export, the weather panel and
``@act`` mentions in comments. A title the author typed is still shown. See UrbanLens#303 ("a hidden stop shows its place's name").
"""

from __future__ import annotations

import datetime
import importlib
import json
import string
from typing import Any
from unittest import mock

from django.apps import apps
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.calendar_sync.model import (
    CalendarSyncDirection,
    GoogleCalendarAccount,
    TripCalendarLink,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment, TripMembership
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.trips.calendar_sync import activity_to_event_body, import_events_as_trips
from urbanlens.dashboard.services.trips.trip_activities import build_activity_rows, create_activity, update_activity
from urbanlens.dashboard.services.trips.trip_comments import build_comment_tree
from urbanlens.dashboard.services.trips.trip_visibility import (
    HIDDEN_ACTIVITY_TITLE,
    masked_activity_title,
    shown_activity_title,
    viewer_hidden_activity_ids,
)

_PLACE_NAME = "Packard Plant"
_TYPED = "Meet at the gate"
_IMPORTED_LOCATION = "123 Factory Rd, Utica, NY"
_IMPORT_NOTE = "Location from the imported Google Calendar event."
_WHEN = datetime.datetime(2026, 11, 6, 9, 0, tzinfo=datetime.UTC)
_MIGRATION = importlib.import_module("urbanlens.dashboard.migrations.0069_calendar_privacy_followups")


class _HiddenStopCase(TestCase):
    """A trip whose member ``mate`` lets no one see their stops, viewed by ``viewer``, a member who may not."""

    def setUp(self) -> None:
        super().setUp()
        self.viewer_user = User.objects.create_user(username="p338-viewer")
        self.viewer = Profile.objects.get(user=self.viewer_user)
        self.mate_user = User.objects.create_user(username="p338-mate")
        self.mate = Profile.objects.get(user=self.mate_user)
        Profile.objects.filter(pk=self.mate.pk).update(
            trip_pin_location_visibility=VisibilityChoice.NO_ONE, comment_visibility=VisibilityChoice.ANYONE
        )
        self.mate.refresh_from_db()
        self.trip = Trip.objects.create(
            name="Long weekend",
            creator=self.viewer,
            start_date=datetime.date(2026, 11, 6),
            end_date=datetime.date(2026, 11, 8),
        )
        for profile in (self.viewer, self.mate):
            TripMembership.objects.create(
                trip=self.trip, profile=profile, status=TripMembership.STATUS_JOINED, rsvp="yes"
            )

    def _searched_stop(
        self, *, title: str | None = None, by: Profile | None = None, when: datetime.datetime = _WHEN
    ) -> TripActivity:
        """A stop added the way the place-search picker adds one."""
        return create_activity(
            self.trip,
            by or self.mate,
            title=title,
            place={"geocoded_lat": "42.380", "geocoded_lng": "-83.035", "geocoded_name": _PLACE_NAME},
            scheduled_at=when,
        )

    def _row(self, activity: TripActivity, viewer: Profile) -> dict[str, Any]:
        return next(
            row
            for row in build_activity_rows(self.trip, viewer, include_legs=False)
            if row["activity"].pk == activity.pk
        )

    def _api_activities(self, user: User) -> str:
        api_key, raw_key = generate_api_key(user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.TRIPS_READ.value])
        response = self.client.get(
            reverse("external_api:trips.activities", args=[self.trip.slug]), HTTP_AUTHORIZATION=f"Bearer {raw_key}"
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class APlaceSearchNameIsRecordedAsSuchTests(_HiddenStopCase):
    def test_an_untitled_stop_from_a_place_search_takes_the_name_and_marks_it(self) -> None:
        activity = self._searched_stop()

        self.assertEqual((activity.title, activity.title_from_place), (_PLACE_NAME, True))

    def test_a_typed_title_is_not_marked(self) -> None:
        activity = self._searched_stop(title=_TYPED)

        self.assertEqual((activity.title, activity.title_from_place), (_TYPED, False))

    def test_typing_a_new_title_unmarks_it_and_an_edit_that_keeps_it_does_not(self) -> None:
        activity = self._searched_stop()

        update_activity(self.trip, self.mate, activity.pk, changes={"notes": "Bring a torch", "title": _PLACE_NAME})
        activity.refresh_from_db()
        self.assertTrue(activity.title_from_place)

        update_activity(self.trip, self.mate, activity.pk, changes={"title": _TYPED})
        activity.refresh_from_db()
        self.assertEqual((activity.title, activity.title_from_place), (_TYPED, False))

    def test_an_editor_who_was_never_shown_the_title_does_not_clear_it_by_saving(self) -> None:
        """The edit dialog posts every field, and the title field of a hidden stop is blank for whoever may not see it."""
        activity = self._searched_stop()
        TripMembership.objects.filter(trip=self.trip, profile=self.viewer).update(is_organizer=True)

        update_activity(self.trip, self.viewer, activity.pk, changes={"title": "", "notes": "Gate code 1234"})

        activity.refresh_from_db()
        self.assertEqual(
            (activity.title, activity.title_from_place, activity.notes), (_PLACE_NAME, True, "Gate code 1234")
        )

    def test_a_title_an_editor_who_may_not_see_it_types_is_theirs_whether_or_not_it_matches(self) -> None:
        """Otherwise a matching guess would leave the title masked and a wrong one would not: an oracle for the name."""
        TripMembership.objects.filter(trip=self.trip, profile=self.viewer).update(is_organizer=True)
        for guess in (_PLACE_NAME, "Fisher Body 21"):
            with self.subTest(guess=guess):
                activity = self._searched_stop()

                update_activity(self.trip, self.viewer, activity.pk, changes={"title": guess})

                activity.refresh_from_db()
                self.assertEqual((activity.title, activity.title_from_place), (guess, False))
                self.assertEqual(self._row(activity, self.viewer)["display_title"], guess)

    def test_an_editor_who_may_not_see_the_stop_does_not_clear_its_location_by_saving(self) -> None:
        """The dialog's place is blank for them too; a stop with no location is hidden from no one, name and all."""
        activity = self._searched_stop()
        location_id = activity.location_id
        TripMembership.objects.filter(trip=self.trip, profile=self.viewer).update(is_organizer=True)
        blank_place = {"location_uuid": "", "pin_uuid": "", "geocoded_lat": "", "geocoded_lng": "", "geocoded_name": ""}

        update_activity(
            self.trip, self.viewer, activity.pk, changes={"title": "", "notes": "Gate code 1234", "place": blank_place}
        )

        activity.refresh_from_db()
        self.assertEqual((activity.location_id, activity.notes), (location_id, "Gate code 1234"))
        self.assertEqual(self._row(activity, self.viewer)["display_title"], HIDDEN_ACTIVITY_TITLE)

    def test_a_place_an_editor_who_may_not_see_the_stop_picks_takes_the_old_places_name_with_it(self) -> None:
        """Else moving the stop to a place they may see would show them the name of the one they may not."""
        from urbanlens.dashboard.models.pin.model import Pin

        Profile.objects.filter(pk=self.mate.pk).update(trip_pin_location_visibility=VisibilityChoice.COMMON_PIN)
        activity = self._searched_stop()
        TripMembership.objects.filter(trip=self.trip, profile=self.viewer).update(is_organizer=True)
        their_pin = Pin.objects.create(
            profile=self.viewer, location=Location.objects.create(latitude=40.0, longitude=-80.0)
        )

        update_activity(
            self.trip, self.viewer, activity.pk, changes={"title": "", "place": {"pin_uuid": str(their_pin.uuid)}}
        )

        activity.refresh_from_db()
        self.assertEqual(activity.location_id, their_pin.location_id)
        self.assertFalse(activity.title_from_place)
        row = self._row(activity, self.viewer)
        self.assertNotIn(_PLACE_NAME, json.dumps([row["display_title"], row["display_own_title"]]))

    def test_the_stops_author_still_moves_or_clears_its_place(self) -> None:
        activity = self._searched_stop()

        update_activity(self.trip, self.mate, activity.pk, changes={"place": {}})

        activity.refresh_from_db()
        self.assertEqual((activity.location_id, activity.title, activity.title_from_place), (None, _PLACE_NAME, True))

    def test_an_imported_events_location_is_marked_as_the_places(self) -> None:
        account = GoogleCalendarAccount.objects.create(profile=self.viewer, access_token="a", refresh_token="r")  # noqa: S106 - fixture value
        event = {
            "id": "evt-mill",
            "status": "confirmed",
            "summary": "Mill",
            "location": "123 Factory Rd, Utica, NY",
            "start": {"date": "2026-11-06"},
            "end": {"date": "2026-11-07"},
        }
        with mock.patch(
            "urbanlens.dashboard.services.trips.calendar_sync._events_by_id", return_value={"evt-mill": event}
        ):
            trips, _skipped, _invited = import_events_as_trips(account, ["evt-mill"])

        activity = trips[0].activities.get()
        self.assertEqual((activity.title, activity.title_from_place), ("123 Factory Rd, Utica, NY", True))


class EverySurfaceMasksAPlaceSearchNameTests(_HiddenStopCase):
    def test_the_panel_row_shows_neither_the_name_nor_a_title_field_holding_it(self) -> None:
        row = self._row(self._searched_stop(), self.viewer)

        self.assertTrue(row["effective_location_hidden"])
        self.assertEqual(row["display_title"], HIDDEN_ACTIVITY_TITLE)
        self.assertIsNone(row["display_own_title"])

    def test_the_rendered_panel_carries_the_name_nowhere(self) -> None:
        self._searched_stop()
        self.client.force_login(self.viewer_user)

        response = self.client.get(reverse("trips.activities", kwargs={"trip_slug": self.trip.slug}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _PLACE_NAME)
        self.assertContains(response, HIDDEN_ACTIVITY_TITLE)

    def test_the_api_returns_neither_title_nor_effective_title_naming_the_place(self) -> None:
        activity = self._searched_stop()

        payload = self._api_activities(self.viewer_user)

        self.assertNotIn(_PLACE_NAME, payload)
        row = next(item for item in json.loads(payload)["results"] if item["id"] == activity.pk)
        self.assertEqual(
            (row["title"], row["effective_title"], row["location_hidden"]), (None, HIDDEN_ACTIVITY_TITLE, True)
        )

    def test_the_calendar_event_is_titled_secret_location(self) -> None:
        activity = self._searched_stop()

        body = activity_to_event_body(activity, hidden_activity_ids={activity.pk})

        assert body is not None
        self.assertEqual(body["summary"], f"Long weekend: {HIDDEN_ACTIVITY_TITLE}")

    def test_the_weather_panel_leaves_the_stop_out(self) -> None:
        tomorrow = timezone.now() + datetime.timedelta(days=1)
        hidden = self._searched_stop(when=tomorrow)
        shown = self._searched_stop(by=self.viewer, when=tomorrow)
        self.client.force_login(self.viewer_user)

        with mock.patch("urbanlens.dashboard.controllers.trip._build_activity_forecasts", return_value=[]) as forecasts:
            self.client.get(reverse("trips.weather", kwargs={"trip_slug": self.trip.slug}))

        forecast_ids = {activity.pk for activity in forecasts.call_args.args[0]}
        self.assertIn(shown.pk, forecast_ids)
        self.assertNotIn(hidden.pk, forecast_ids)

    def test_a_comment_mentioning_the_stop_does_not_name_it(self) -> None:
        activity = self._searched_stop()
        TripComment.objects.create(trip=self.trip, author=self.mate, text="Meet at @act:1 at nine")

        seen_by_viewer = build_comment_tree(self.trip, self.viewer)[0]["rendered_text"]
        seen_by_mate = build_comment_tree(self.trip, self.mate)[0]["rendered_text"]

        assert activity.location is not None
        self.assertNotIn(_PLACE_NAME, str(seen_by_viewer))
        self.assertNotIn(str(activity.location.uuid), str(seen_by_viewer))
        self.assertIn(_PLACE_NAME, str(seen_by_mate))
        self.assertIn(str(activity.location.uuid), str(seen_by_mate))

    def test_searching_for_the_name_does_not_find_the_trip(self) -> None:
        """A match would say what the hidden stop is called."""
        from urbanlens.dashboard.services.global_search.parser import parse_query
        from urbanlens.dashboard.services.global_search.providers import TripSearchProvider

        self._searched_stop()

        self.assertEqual(TripSearchProvider().search(self.viewer, parse_query("Packard"), 10), [])
        self.assertEqual(
            [result.title for result in TripSearchProvider().search(self.mate, parse_query("Packard"), 10)],
            ["Long weekend"],
        )

    def test_searching_for_a_typed_title_still_finds_the_trip(self) -> None:
        from urbanlens.dashboard.services.global_search.parser import parse_query
        from urbanlens.dashboard.services.global_search.providers import TripSearchProvider

        self._searched_stop(title=_TYPED)

        self.assertEqual(
            [result.title for result in TripSearchProvider().search(self.viewer, parse_query("gate"), 10)],
            ["Long weekend"],
        )

    def test_the_member_who_may_see_it_still_sees_the_name(self) -> None:
        """Anti-vacuity: the adder's own setting never hides their stop from them."""
        activity = self._searched_stop()

        row = self._row(activity, self.mate)

        self.assertFalse(row["effective_location_hidden"])
        self.assertEqual((row["display_title"], row["display_own_title"]), (_PLACE_NAME, _PLACE_NAME))
        self.assertIn(_PLACE_NAME, self._api_activities(self.mate_user))


class AnImportedEventsLocationIsHiddenAsAPlaceIsTests(_HiddenStopCase):
    """An import stores the event's location as the stop's title and makes no Location of it: the title is the place."""

    def _imported_stop(self, *, by: Profile | None = None) -> TripActivity:
        return TripActivity.objects.create(
            trip=self.trip,
            added_by=by or self.mate,
            title=_IMPORTED_LOCATION,
            title_from_place=True,
            notes=_IMPORT_NOTE,
            scheduled_at=_WHEN,
        )

    def test_a_member_who_may_not_see_the_adders_stops_sees_it_nowhere(self) -> None:
        activity = self._imported_stop()

        row = self._row(activity, self.viewer)
        payload = self._api_activities(self.viewer_user)
        body = activity_to_event_body(activity, hidden_activity_ids=viewer_hidden_activity_ids([activity], self.viewer))

        self.assertTrue(row["effective_location_hidden"])
        self.assertEqual((row["display_title"], row["display_own_title"]), (HIDDEN_ACTIVITY_TITLE, None))
        self.assertNotIn(_IMPORTED_LOCATION, payload)
        assert body is not None
        self.assertNotIn(_IMPORTED_LOCATION, json.dumps(body))

    def test_the_importer_still_sees_it(self) -> None:
        """Anti-vacuity: the adder's own setting never hides their stop from them."""
        activity = self._imported_stop()

        self.assertEqual(self._row(activity, self.mate)["display_title"], _IMPORTED_LOCATION)

    def test_a_typed_title_on_a_stop_with_no_place_is_shown_to_everyone(self) -> None:
        activity = TripActivity.objects.create(trip=self.trip, added_by=self.mate, title=_TYPED, scheduled_at=_WHEN)

        row = self._row(activity, self.viewer)

        self.assertFalse(row["effective_location_hidden"])
        self.assertEqual(row["display_title"], _TYPED)


class CompletingAHiddenStopTests(_HiddenStopCase):
    def test_a_member_who_may_not_see_the_stop_gets_no_visit_suggestion_naming_it(self) -> None:
        from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
        from urbanlens.dashboard.services.trips.trip_activities import complete_activity

        activity = self._searched_stop()

        complete_activity(self.trip, self.mate, activity.pk, completed_date=datetime.date(2026, 10, 1))

        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=self.viewer).exists())

    def test_a_member_who_may_see_it_still_gets_one(self) -> None:
        """Anti-vacuity: the suggestion path itself still runs."""
        from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
        from urbanlens.dashboard.services.trips.trip_activities import complete_activity

        Profile.objects.filter(pk=self.mate.pk).update(trip_pin_location_visibility=VisibilityChoice.ANYONE)
        activity = self._searched_stop()

        complete_activity(self.trip, self.mate, activity.pk, completed_date=datetime.date(2026, 10, 1))

        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=self.viewer).exists())


class TheMemoriesTimelineDoesNotPlaceATripAtAHiddenStopTests(_HiddenStopCase):
    """The timeline puts a trip at its first located stop, and its ``bbox`` filter can find that point."""

    def _trip_event(self, **kwargs: Any) -> Any:
        from urbanlens.dashboard.services.memories.aggregator import get_memory_events

        events = get_memory_events(self.viewer, datetime.date(2026, 11, 1), datetime.date(2026, 11, 30), **kwargs)
        return next((event for event in events if event.type == "trip"), None)

    def test_a_hidden_stop_does_not_place_the_trip(self) -> None:
        from urbanlens.dashboard.services.memories.aggregator import BBox

        self._searched_stop()

        event = self._trip_event()
        assert event is not None
        self.assertEqual((event.latitude, event.longitude), (None, None))
        self.assertIsNone(self._trip_event(bbox=BBox(42.37, -83.04, 42.39, -83.03)))

    def test_a_stop_the_viewer_may_see_still_does(self) -> None:
        """Anti-vacuity."""
        self._searched_stop(by=self.viewer)

        event = self._trip_event()
        assert event is not None
        self.assertAlmostEqual(float(event.latitude), 42.38, places=2)

    def test_the_feed_checks_every_trips_stops_at_once_not_one_trip_at_a_time(self) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from urbanlens.dashboard.services.memories.aggregator import _trips_for_range

        Profile.objects.filter(pk=self.mate.pk).update(trip_pin_location_visibility=VisibilityChoice.COMMON_FRIEND)

        def queries() -> int:
            with CaptureQueriesContext(connection) as captured:
                events = list(
                    _trips_for_range(
                        self.viewer, datetime.date(2026, 11, 1), datetime.date(2026, 11, 30), None, limit=50
                    )
                )
            self.assertTrue(all(event.latitude is None for event in events))
            return len(captured)

        self._searched_stop()
        one_trip = queries()
        for number in range(9):
            self.trip = Trip.objects.create(
                name=f"Weekend {number}", creator=self.viewer, start_date=datetime.date(2026, 11, 6)
            )
            for profile in (self.viewer, self.mate):
                TripMembership.objects.create(trip=self.trip, profile=profile, status=TripMembership.STATUS_JOINED)
            self._searched_stop()

        self.assertEqual(queries(), one_trip)


class ATypedTitleIsStillShownTests(_HiddenStopCase):
    def test_every_surface_shows_a_typed_title_on_a_hidden_stop(self) -> None:
        activity = self._searched_stop(title=_TYPED)

        row = self._row(activity, self.viewer)
        body = activity_to_event_body(activity, hidden_activity_ids={activity.pk})
        payload = self._api_activities(self.viewer_user)

        self.assertEqual((row["display_title"], row["display_own_title"]), (_TYPED, _TYPED))
        assert body is not None
        self.assertEqual(body["summary"], f"Long weekend: {_TYPED}")
        api_row = next(item for item in json.loads(payload)["results"] if item["id"] == activity.pk)
        self.assertEqual((api_row["title"], api_row["effective_title"]), (_TYPED, _TYPED))
        self.assertNotIn(_PLACE_NAME, payload)


class TheSurfacesAgreeTests(SimpleTestCase):
    """Whatever the title and wherever it came from, the panel, the API and the calendar show the same name."""

    _token = st.text(alphabet=string.ascii_letters, min_size=3, max_size=12)

    @given(
        place=_token.map(lambda text: f"Zq{text}"),
        typed=_token.map(lambda text: f"Yt{text}"),
        title_kind=st.sampled_from(["typed", "from place", "none"]),
        hidden=st.booleans(),
    )
    def test_a_hidden_stop_is_never_named_after_its_place(
        self, place: str, typed: str, title_kind: str, hidden: bool
    ) -> None:
        title = {"typed": typed, "from place": place, "none": None}[title_kind]
        activity = TripActivity(
            trip=Trip(name="Mill weekend"),
            location=Location(latitude=42.38, longitude=-83.03, official_name=place),
            title=title,
            title_from_place=title_kind == "from place",
            location_hidden=hidden,
            scheduled_at=_WHEN,
        )

        shown = masked_activity_title(activity, hidden=hidden)
        own = shown_activity_title(activity, hidden=hidden)
        body = activity_to_event_body(activity)

        assert body is not None
        self.assertEqual(body["summary"], f"Mill weekend: {shown}")
        if hidden:
            self.assertNotIn("Zq", shown + (own or "") + json.dumps(body))
            self.assertEqual(shown, typed if title_kind == "typed" else HIDDEN_ACTIVITY_TITLE)
        else:
            self.assertEqual(shown, title or place)
            self.assertEqual(own, title)


class BackfillTests(TestCase):
    """A located stop's stored title may be its place search's name, and cannot be told from a typed one, so it is taken
    to be the place's until its author types one. A stop with no place has a place's name only from an import."""

    def test_every_title_that_may_be_a_places_is_marked_and_no_other(self) -> None:
        profile = User.objects.create_user(username="p338-backfill").profile
        trip = Trip.objects.create(name="Old trip", creator=profile)
        mill = Location.objects.create(latitude=42.38, longitude=-83.03)
        located = TripActivity.objects.create(trip=trip, added_by=profile, location=mill, title="Meet at the gate")
        typed = TripActivity.objects.create(trip=trip, added_by=profile, title="Drive home")
        imported = TripActivity.objects.create(
            trip=trip, added_by=profile, title=_IMPORTED_LOCATION, notes=_IMPORT_NOTE
        )
        timed_import = TripActivity.objects.create(
            trip=trip, added_by=profile, title=_IMPORTED_LOCATION, notes="Bring boots"
        )
        GoogleCalendarAccount.objects.create(profile=profile, access_token="a", refresh_token="r")  # noqa: S106 - fixture value
        TripCalendarLink.objects.create(
            trip=trip,
            activity=timed_import,
            profile=profile,
            google_event_id="evt",
            direction=CalendarSyncDirection.IMPORTED,
        )
        blank = TripActivity.objects.create(trip=trip, added_by=profile, location=mill, title="  ")
        untitled = TripActivity.objects.create(trip=trip, added_by=profile, location=mill, title=None)
        rows = [located, typed, imported, timed_import, blank, untitled]

        _MIGRATION.mark_stored_titles_as_possibly_the_places(apps, None)

        flags = dict(TripActivity.objects.filter(pk__in=[row.pk for row in rows]).values_list("pk", "title_from_place"))
        self.assertEqual([flags[row.pk] for row in rows], [True, False, True, True, False, False])
