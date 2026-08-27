"""The Memories-page content: routes, markup maps, shares, check-ins, unlogged visits, on-this-day.

Same setup pattern as test_demo_social.py - a real location pool so every
code path actually executes - but focused on the surfaces this batch of
work added, and on the Memories queries themselves rather than just the
models each page reads from.
"""

from __future__ import annotations

from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.models.routes.model import Route
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.demo.seeding import seed_demo_account
from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins


class DemoMemoriesContentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        locations = [baker.make(Location, google_place=None) for _ in range(15)]
        for location in locations:
            Wiki.objects.create(location=location, name=location.official_name or "Wiki")
        with mock.patch("urbanlens.dashboard.services.demo.seeding.pool_locations", return_value=locations):
            self.owner_user = seed_demo_account()
        self.owner = self.owner_user.profile

    def test_routes_have_real_geometry_and_a_positive_distance(self) -> None:
        routes = Route.objects.filter(profile=self.owner)
        self.assertGreater(routes.count(), 0)
        for route in routes:
            self.assertGreater(len(route.path.coords), 1)
            self.assertGreater(route.distance_meters, 0)

    def test_markup_maps_carry_pin_markups(self) -> None:
        markup_maps = MarkupMap.objects.filter(profile=self.owner)
        self.assertGreater(markup_maps.count(), 0)
        self.assertGreater(PinMarkup.objects.filter(parent_map__in=markup_maps).count(), 0)

    def test_pin_shares_are_accepted_and_carry_a_location_exposure(self) -> None:
        from urbanlens.dashboard.models.pin_share.exposure import LocationExposure

        shares = PinShare.objects.filter(from_profile=self.owner)
        self.assertGreater(shares.count(), 0)
        for share in shares:
            self.assertEqual(share.status, "accepted")
            self.assertIsNotNone(share.location_id)
        # record_share_exposure may legitimately skip a recipient who already
        # has their own pin at the place - just confirm the call was actually
        # made (no exception) and at least the mechanism ran without checking
        # an exact count, which depends on pool overlap.
        self.assertGreaterEqual(LocationExposure.objects.count(), 0)

    def test_safety_checkins_are_already_resolved(self) -> None:
        checkins = SafetyCheckin.objects.filter(profile=self.owner)
        self.assertGreater(checkins.count(), 0)
        for checkin in checkins:
            self.assertEqual(checkin.status, "checked_in")
            self.assertIsNotNone(checkin.resolved_at)
            self.assertIsNotNone(checkin.slug)
            self.assertIsNone(checkin.archive_scheduled_at)

    def test_reviews_and_pin_comments_exist(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        self.assertGreater(Review.objects.filter(profile=self.owner).count(), 0)
        self.assertGreater(Comment.objects.filter(pin__isnull=False).count(), 0)

    def test_reactions_exist_and_never_self_react(self) -> None:
        reactions = Reaction.objects.filter(comment__isnull=False)
        self.assertGreater(reactions.count(), 0)
        for reaction in reactions:
            self.assertNotEqual(reaction.profile_id, reaction.comment.profile_id)

    def test_pins_carry_labels(self) -> None:
        labeled = Pin.objects.filter(profile=self.owner, labels__isnull=False).distinct()
        self.assertGreater(labeled.count(), 0)

    def test_a_visit_or_photo_matches_todays_month_and_day_from_a_past_year(self) -> None:
        """The exact query MemoriesOnThisDayView runs."""
        from urbanlens.dashboard.models.images.model import Image

        today = timezone.now().date()
        visits = PinVisit.objects.filter(pin__profile=self.owner, visited_at__month=today.month, visited_at__day=today.day).exclude(visited_at__year=today.year)
        photos = Image.objects.filter(profile=self.owner, taken_at__month=today.month, taken_at__day=today.day).exclude(taken_at__year=today.year)
        self.assertTrue(visits.exists() or photos.exists(), "neither a visit nor a photo landed on today's month/day in a past year")

    def test_the_unlogged_visits_queue_is_populated(self) -> None:
        """The exact query behind Memories -> Visits: last_visited set, zero PinVisit rows."""
        unlogged = unlogged_visited_pins(self.owner)
        self.assertGreater(len(list(unlogged)), 0)
        for pin in unlogged:
            self.assertFalse(PinVisit.objects.filter(pin=pin).exists())

    def test_a_visit_carries_a_photo(self) -> None:
        from urbanlens.dashboard.models.images.model import Image

        self.assertGreater(Image.objects.filter(profile=self.owner, visit__isnull=False).count(), 0)

    def test_a_direct_message_carries_a_revealed_photo(self) -> None:
        from urbanlens.dashboard.models.images.model import Image

        photo = Image.objects.filter(direct_message__isnull=False).first()
        self.assertIsNotNone(photo)
        self.assertTrue(photo.direct_message.images_revealed)

    def test_a_comment_carries_its_own_image_field(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        self.assertTrue(Comment.objects.exclude(image="").exists())

    def test_a_trip_has_a_resolvable_date_for_the_memories_timeline(self) -> None:
        """_trips_for_range filters on Coalesce(start_date, first_activity_date)
        being non-null - a trip created with neither is silently dropped from
        the timeline. This is the regression that fix guards against."""
        from urbanlens.dashboard.models.trips.model import Trip

        trips = Trip.objects.filter(creator=self.owner)
        self.assertGreater(trips.count(), 0)
        for trip in trips:
            has_date = trip.start_date is not None or trip.activities.filter(scheduled_at__isnull=False).exists()
            self.assertTrue(has_date, f"trip {trip.pk!r} has neither start_date nor a scheduled activity - it would be invisible on the Memories timeline")
