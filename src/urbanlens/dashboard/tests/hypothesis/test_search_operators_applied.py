"""Real filtering/sorting effect of the `label:`/`by:`/`has:`/`is:`/`sort:` operators.

`test_search_operators.py` only tests parsing (ParsedQuery fields, describe_filters() chips).
These operators were parsed correctly but never consumed by any provider - this file is the
"they actually change the result set" counterpart, added when that gap was closed.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search import GlobalSearchEngine


def _titles(response, slug: str) -> list[str]:
    for group in response.groups:
        if group.meta.slug == slug:
            return [r.title for r in group.results]
    return []


class LabelOperatorTests(TestCase):
    """`label:`/`-label:` - only Pin, Image, and Wiki carry a Label M2M."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.tag = Label.objects.create(name="Rooftop", kind=KIND_TAG)
        self.other_tag = Label.objects.create(name="Basement", kind=KIND_TAG)

    def test_label_narrows_pins_to_ones_carrying_it(self):
        tagged = baker.make(Pin, profile=self.profile, name="Tagged Spot")
        tagged.labels.add(self.tag)
        untagged = baker.make(Pin, profile=self.profile, name="Untagged Spot")

        response = GlobalSearchEngine().search(self.profile, "label:rooftop")

        titles = _titles(response, "pins")
        self.assertIn(tagged.name, titles)
        self.assertNotIn(untagged.name, titles)

    def test_label_is_case_insensitive(self):
        pin = baker.make(Pin, profile=self.profile, name="Cased Spot")
        pin.labels.add(self.tag)

        response = GlobalSearchEngine().search(self.profile, "label:ROOFTOP")

        self.assertIn("Cased Spot", _titles(response, "pins"))

    def test_exclude_label_drops_pins_carrying_it(self):
        excluded = baker.make(Pin, profile=self.profile, name="Basement Spot")
        excluded.labels.add(self.other_tag)
        kept = baker.make(Pin, profile=self.profile, name="Attic Spot")

        response = GlobalSearchEngine().search(self.profile, "-label:basement")

        titles = _titles(response, "pins")
        self.assertIn(kept.name, titles)
        self.assertNotIn(excluded.name, titles)

    def test_label_and_exclude_label_compose(self):
        both = baker.make(Pin, profile=self.profile, name="Both Spot")
        both.labels.add(self.tag, self.other_tag)
        only_rooftop = baker.make(Pin, profile=self.profile, name="Only Rooftop Spot")
        only_rooftop.labels.add(self.tag)

        response = GlobalSearchEngine().search(self.profile, "label:rooftop -label:basement")

        titles = _titles(response, "pins")
        self.assertIn(only_rooftop.name, titles)
        self.assertNotIn(both.name, titles)

    def test_label_narrows_photos_to_ones_carrying_it(self):
        from urbanlens.dashboard.models.images import Image

        tagged = baker.make(Image, profile=self.profile, caption="Tagged Photo", image="pin_images/a.jpg")
        tagged.labels.add(self.tag)
        baker.make(Image, profile=self.profile, caption="Untagged Photo", image="pin_images/b.jpg")

        response = GlobalSearchEngine().search(self.profile, "label:rooftop")

        titles = _titles(response, "photos")
        self.assertIn("Tagged Photo", titles)
        self.assertNotIn("Untagged Photo", titles)

    def test_label_narrows_wikis_to_ones_carrying_it(self):
        location = Location.objects.create(latitude=39.10, longitude=-84.51, official_name="Wiki Spot")
        baker.make(Pin, profile=self.profile, location=location)
        wiki = baker.make(Wiki, location=location, name="Tagged Wiki")
        wiki.labels.add(self.tag)

        response = GlobalSearchEngine().search(self.profile, "label:rooftop")

        self.assertIn("Tagged Wiki", _titles(response, "wikis"))

    def test_does_not_leak_another_users_pin_via_label(self):
        other = baker.make(User).profile
        other_pin = baker.make(Pin, profile=other, name="Someone Else's Roof")
        other_pin.labels.add(self.tag)

        response = GlobalSearchEngine().search(self.profile, "label:rooftop")

        self.assertNotIn("Someone Else's Roof", _titles(response, "pins"))


class AuthorOperatorTests(TestCase):
    """`by:`/`author:` - "me" resolves to the searching profile; a name reuses person_match()."""

    def setUp(self):
        self.user = baker.make(User, username="alice")
        self.profile = self.user.profile
        self.other_user = baker.make(User, username="bob")
        self.other_profile = self.other_user.profile

    def test_by_me_finds_own_pin(self):
        baker.make(Pin, profile=self.profile, name="Alices Spot")

        response = GlobalSearchEngine().search(self.profile, "by:me")

        self.assertIn("Alices Spot", _titles(response, "pins"))

    def test_by_someone_else_never_returns_own_pins(self):
        """Pins are owner-only by construction - `by:` can't be used to fish
        for another profile's private pin, since the base queryset never
        contains anything but the searcher's own rows."""
        baker.make(Pin, profile=self.profile, name="Alices Spot")

        response = GlobalSearchEngine().search(self.profile, "by:bob")

        self.assertEqual(_titles(response, "pins"), [])

    def test_by_name_finds_own_trip_created_by_that_name(self):
        from urbanlens.dashboard.models.trips import Trip

        baker.make(Trip, creator=self.profile, name="Alices Trip")

        response = GlobalSearchEngine().search(self.profile, "by:alice")

        self.assertIn("Alices Trip", _titles(response, "trips"))

    def test_by_someone_else_excludes_a_trip_i_created(self):
        from urbanlens.dashboard.models.trips import Trip

        baker.make(Trip, creator=self.profile, name="Alices Trip")

        response = GlobalSearchEngine().search(self.profile, "by:bob")

        self.assertNotIn("Alices Trip", _titles(response, "trips"))


class PinHasOperatorTests(TestCase):
    """`has:`/`-has:` - Pin-only, per its one documented example."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_has_photos_narrows_to_pins_with_a_photo(self):
        from urbanlens.dashboard.models.images import Image

        with_photo = baker.make(Pin, profile=self.profile, name="Photographed Spot")
        baker.make(Image, pin=with_photo, profile=self.profile, image="pin_images/a.jpg")
        baker.make(Pin, profile=self.profile, name="Bare Spot")

        response = GlobalSearchEngine().search(self.profile, "has:photos")

        titles = _titles(response, "pins")
        self.assertIn("Photographed Spot", titles)
        self.assertNotIn("Bare Spot", titles)

    def test_negated_has_photos_excludes_pins_with_a_photo(self):
        from urbanlens.dashboard.models.images import Image

        with_photo = baker.make(Pin, profile=self.profile, name="Photographed Spot")
        baker.make(Image, pin=with_photo, profile=self.profile, image="pin_images/a.jpg")
        bare = baker.make(Pin, profile=self.profile, name="Bare Spot")

        response = GlobalSearchEngine().search(self.profile, "-has:photos")

        titles = _titles(response, "pins")
        self.assertIn(bare.name, titles)
        self.assertNotIn("Photographed Spot", titles)

    def test_has_wiki_narrows_to_pins_linked_to_a_wiki(self):
        location = Location.objects.create(latitude=39.10, longitude=-84.51)
        wiki = baker.make(Wiki, location=location)
        linked = baker.make(Pin, profile=self.profile, location=location, wiki=wiki, name="Linked Spot")
        baker.make(Pin, profile=self.profile, name="Unlinked Spot")

        response = GlobalSearchEngine().search(self.profile, "has:wiki")

        titles = _titles(response, "pins")
        self.assertIn(linked.name, titles)
        self.assertNotIn("Unlinked Spot", titles)

    def test_has_labels_narrows_to_pins_carrying_any_label(self):
        tagged = baker.make(Pin, profile=self.profile, name="Tagged Spot")
        tagged.labels.add(Label.objects.create(name="Rooftop", kind=KIND_TAG))
        baker.make(Pin, profile=self.profile, name="Untagged Spot")

        response = GlobalSearchEngine().search(self.profile, "has:labels")

        titles = _titles(response, "pins")
        self.assertIn(tagged.name, titles)
        self.assertNotIn("Untagged Spot", titles)


class PinVisitedStateTests(TestCase):
    """`is:visited`/`is:unvisited` reuse PinQuerySet.visited()/never_visited()."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_is_visited_narrows_to_visited_pins(self):
        visited = baker.make(Pin, profile=self.profile, name="Been There", last_visited=timezone.now())
        baker.make(Pin, profile=self.profile, name="Not Yet")

        response = GlobalSearchEngine().search(self.profile, "is:visited")

        titles = _titles(response, "pins")
        self.assertIn(visited.name, titles)
        self.assertNotIn("Not Yet", titles)

    def test_is_unvisited_narrows_to_unvisited_pins(self):
        baker.make(Pin, profile=self.profile, name="Been There", last_visited=timezone.now())
        unvisited = baker.make(Pin, profile=self.profile, name="Not Yet")

        response = GlobalSearchEngine().search(self.profile, "is:unvisited")

        titles = _titles(response, "pins")
        self.assertIn(unvisited.name, titles)
        self.assertNotIn("Been There", titles)


class TripStateOperatorTests(TestCase):
    """`is:upcoming`/`is:past`, via raw start_date/end_date."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_is_upcoming_narrows_to_future_trips(self):
        from urbanlens.dashboard.models.trips import Trip

        today = timezone.localdate()
        upcoming = baker.make(Trip, creator=self.profile, name="Next Month", start_date=today + timedelta(days=30))
        baker.make(Trip, creator=self.profile, name="Last Month", end_date=today - timedelta(days=30))

        response = GlobalSearchEngine().search(self.profile, "is:upcoming")

        titles = _titles(response, "trips")
        self.assertIn(upcoming.name, titles)
        self.assertNotIn("Last Month", titles)

    def test_is_past_narrows_to_completed_trips(self):
        from urbanlens.dashboard.models.trips import Trip

        today = timezone.localdate()
        baker.make(Trip, creator=self.profile, name="Next Month", start_date=today + timedelta(days=30))
        past = baker.make(Trip, creator=self.profile, name="Last Month", end_date=today - timedelta(days=30))

        response = GlobalSearchEngine().search(self.profile, "is:past")

        titles = _titles(response, "trips")
        self.assertIn(past.name, titles)
        self.assertNotIn("Next Month", titles)

    def test_a_dateless_trip_matches_neither_state(self):
        from urbanlens.dashboard.models.trips import Trip

        baker.make(Trip, creator=self.profile, name="Someday", start_date=None, end_date=None)

        upcoming = GlobalSearchEngine().search(self.profile, "is:upcoming")
        past = GlobalSearchEngine().search(self.profile, "is:past")

        self.assertNotIn("Someday", _titles(upcoming, "trips"))
        self.assertNotIn("Someday", _titles(past, "trips"))


class SafetyArchivedStateTests(TestCase):
    """`is:archived` - real for SafetyCheckin (`hasattr(checkin, "archive")`), unlike most types."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_is_archived_narrows_to_archived_checkins(self):
        from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinArchive

        archived = baker.make(SafetyCheckin, profile=self.profile, title="Old Hike")
        SafetyCheckinArchive.objects.create(checkin=archived, ciphertext="x", nonce="y", sealed_key="z", key_bundle_version=1)
        baker.make(SafetyCheckin, profile=self.profile, title="Live Hike")

        response = GlobalSearchEngine().search(self.profile, "is:archived")

        titles = _titles(response, "safety")
        self.assertIn("Old Hike", titles)
        self.assertNotIn("Live Hike", titles)


class SortOperatorTests(TestCase):
    """`sort:` - engine.py trusts each provider's own ordering instead of re-sorting by score."""

    def setUp(self):
        self.user = baker.make(User)
        self.profile = self.user.profile

    def test_sort_most_visited_ranks_the_more_visited_pin_first(self):
        from urbanlens.dashboard.models.visits import PinVisit

        popular = baker.make(Pin, profile=self.profile, name="Popular Spot")
        baker.make(PinVisit, pin=popular, _quantity=3)
        quiet = baker.make(Pin, profile=self.profile, name="Quiet Spot")
        baker.make(PinVisit, pin=quiet, _quantity=1)

        response = GlobalSearchEngine().search(self.profile, "pins sort:most-visited")

        titles = _titles(response, "pins")
        self.assertLess(titles.index("Popular Spot"), titles.index("Quiet Spot"))

    def test_sort_nearest_ranks_the_closer_pin_first(self):
        self.profile.map_custom_latitude = "39.10"
        self.profile.map_custom_longitude = "-84.51"
        self.profile.save()
        near_location = baker.make(Location, latitude="39.11", longitude="-84.52")
        far_location = baker.make(Location, latitude="51.50", longitude="-0.12")
        baker.make(Pin, profile=self.profile, location=near_location, name="Close Spot")
        baker.make(Pin, profile=self.profile, location=far_location, name="Far Spot")

        response = GlobalSearchEngine().search(self.profile, "pins sort:nearest")

        titles = _titles(response, "pins")
        self.assertLess(titles.index("Close Spot"), titles.index("Far Spot"))

    def test_sort_updated_overrides_default_relevance_ordering(self):
        older = baker.make(Pin, profile=self.profile, name="Older Match")
        newer = baker.make(Pin, profile=self.profile, name="Newer Match")
        Pin.objects.filter(pk=older.pk).update(updated=timezone.now() - timedelta(days=5))
        Pin.objects.filter(pk=newer.pk).update(updated=timezone.now())

        response = GlobalSearchEngine().search(self.profile, "pins sort:updated")

        titles = _titles(response, "pins")
        self.assertLess(titles.index("Newer Match"), titles.index("Older Match"))
