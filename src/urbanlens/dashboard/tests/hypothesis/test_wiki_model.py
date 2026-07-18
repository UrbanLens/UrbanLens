"""Tests for the Wiki model and the Location/Wiki split.

Covers the core invariants of the community-wiki extraction:
- Wiki holds the community name; Location keeps only address/official_name.
- ``Location.display_name`` / ``Pin.effective_name`` resolve through the wiki.
- ``Wiki.objects.get_or_create_for_location`` is lazy and idempotent.
- Comments are pin-XOR-wiki; community aliases/edits/detail-pins live on Wiki.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki


class WikiLocationRelationTests(TestCase):
    """Wiki links 1:1 to a Location and proxies its address."""

    def test_get_or_create_is_lazy_and_idempotent(self) -> None:
        loc = baker.make(Location, official_name="Old Mill", latitude="40.0", longitude="-74.0")
        wiki1, created1 = Wiki.objects.get_or_create_for_location(loc)
        wiki2, created2 = Wiki.objects.get_or_create_for_location(loc)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(wiki1.pk, wiki2.pk)
        # Name seeds from the location's official_name.
        self.assertEqual(wiki1.name, "Old Mill")

    def test_wiki_proxies_location_coordinates(self) -> None:
        loc = baker.make(Location, latitude="41.5", longitude="-72.25")
        wiki = baker.make(Wiki, location=loc, name="Ruin")
        self.assertEqual(float(wiki.latitude), 41.5)
        self.assertEqual(float(wiki.longitude), -72.25)

    def test_reverse_accessor(self) -> None:
        loc = baker.make(Location, latitude="42.0", longitude="-71.0")
        wiki = baker.make(Wiki, location=loc, name="Place")
        loc.refresh_from_db()
        self.assertEqual(loc.wiki.pk, wiki.pk)

    def test_unnamed_location_placeholder_includes_area_when_known(self) -> None:
        """Matches Location.display_name's own area-suffixed placeholder, so an
        unnamed wiki reads as "Unnamed Location in Albany, NY" instead of a
        bare "Unnamed Location" that's indistinguishable from every other one."""
        loc = baker.make(Location, official_name="", city="Albany", state="NY", country="USA", latitude="40.0", longitude="-74.0")
        wiki, _created = Wiki.objects.get_or_create_for_location(loc)
        self.assertEqual(wiki.name, "Unnamed Location in Albany, NY")

    def test_unnamed_location_placeholder_stays_bare_without_area_data(self) -> None:
        loc = baker.make(Location, official_name="", city="", state="", country="", latitude="40.0", longitude="-74.0")
        wiki, _created = Wiki.objects.get_or_create_for_location(loc)
        self.assertEqual(wiki.name, "Unnamed Location")


class EnrichWikiLocationNameTests(TestCase):
    """tasks.enrich_wiki_location's placeholder-name replacement.

    Previously verified by code review only (noted as a test gap when the
    area-suffixed placeholder shipped) - these lock in the three behaviors
    that matter: any non-meaningful seeded name gets replaced, a placeholder
    seeded from an OLDER area_label (address backfill may have changed the
    location's city/state since the wiki was created) still gets replaced,
    and a real community name is never touched.
    """

    def _run(self, wiki: Wiki, resolved_name: str | None = "Resolved Factory") -> None:
        from urbanlens.dashboard import tasks

        with (
            patch("urbanlens.dashboard.tasks.update_task_progress"),
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService.ensure_linked"),
            patch("urbanlens.dashboard.services.locations.google.PlaceNameResolverChain.resolve", return_value=resolved_name),
            patch("urbanlens.dashboard.services.locations.boundaries.boundary_generation_ran", return_value=True),
        ):
            tasks.enrich_wiki_location(wiki.pk)

    def _location(self, **kwargs) -> Location:
        defaults = {"official_name": "", "latitude": "40.0", "longitude": "-74.0", "google_place": None}
        return baker.make(Location, **{**defaults, **kwargs})

    def test_bare_placeholder_is_replaced_with_the_resolved_name(self) -> None:
        wiki = baker.make(Wiki, location=self._location(), name="Unnamed Location")
        self._run(wiki)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Resolved Factory")

    def test_area_suffixed_placeholder_from_a_stale_area_label_is_still_replaced(self) -> None:
        """The wiki was seeded "Unnamed Location in Albany, NY" but the
        location's address data has since changed, so that exact string can no
        longer be reconstructed from the CURRENT area_label - the update must
        key on the name actually read, not a rebuilt placeholder set."""
        location = self._location(city="Troy", state="NY", country="USA")
        wiki = baker.make(Wiki, location=location, name="Unnamed Location in Albany, NY")
        self._run(wiki)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Resolved Factory")

    def test_meaningful_community_name_is_never_replaced(self) -> None:
        wiki = baker.make(Wiki, location=self._location(), name="Beloved Community Ruin")
        self._run(wiki)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Beloved Community Ruin")

    def test_placeholder_survives_when_nothing_resolves(self) -> None:
        wiki = baker.make(Wiki, location=self._location(), name="Unnamed Location")
        self._run(wiki, resolved_name=None)
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Unnamed Location")

    def test_official_name_is_preferred_over_live_resolution(self) -> None:
        location = self._location(official_name="Official Mill")
        wiki = baker.make(Wiki, location=location, name="Unnamed Location")
        self._run(wiki, resolved_name="Should Not Be Used")
        wiki.refresh_from_db()
        self.assertEqual(wiki.name, "Official Mill")


class DisplayNameResolutionTests(TestCase):
    """Location.display_name and Pin.effective_name resolve through the wiki."""

    def test_display_name_prefers_wiki_name(self) -> None:
        loc = baker.make(Location, official_name="Official", latitude="40.1", longitude="-74.1")
        baker.make(Wiki, location=loc, name="Community Name")
        loc.refresh_from_db()
        self.assertEqual(loc.display_name, "Community Name")

    def test_display_name_falls_back_to_official_name(self) -> None:
        loc = baker.make(Location, official_name="Official Only", latitude="40.2", longitude="-74.2")
        self.assertEqual(loc.display_name, "Official Only")

    def test_pin_effective_name_uses_wiki_name(self) -> None:
        loc = baker.make(Location, official_name="Official", latitude="40.3", longitude="-74.3")
        baker.make(Wiki, location=loc, name="Wiki Title")
        profile = baker.make("auth.User").profile
        pin = baker.make(Pin, profile=profile, location=loc, name=None)
        self.assertEqual(pin.effective_name, "Wiki Title")

    def test_pin_own_name_wins(self) -> None:
        loc = baker.make(Location, official_name="Official", latitude="40.4", longitude="-74.4")
        baker.make(Wiki, location=loc, name="Wiki Title")
        profile = baker.make("auth.User").profile
        pin = baker.make(Pin, profile=profile, location=loc, name="My Label")
        self.assertEqual(pin.effective_name, "My Label")


class WikiCommentConstraintTests(TestCase):
    """Comments attach to exactly one of pin / wiki."""

    def test_wiki_comment(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        # Profile is auto-created via User's post_save signal - pass it
        # explicitly rather than letting baker create a second one for the FK,
        # which would collide on the unique constraint.
        profile = baker.make("auth.User").profile
        wiki = baker.make(Wiki, name="W")
        comment = baker.make(Comment, profile=profile, wiki=wiki, pin=None, parent=None, text="hi")
        self.assertEqual(list(Comment.objects.for_wiki(wiki)), [comment])


class WikiEffectiveDateLastActiveTests(TestCase):
    """effective_date_last_active returns date_last_active, infers from abandoned, or None.

    The community "last active"/"abandoned" dates moved from Location to Wiki in
    the wiki split; these are pure-property tests on unsaved Wiki instances.
    """

    def _wiki(self, **kwargs) -> Wiki:
        wiki = Wiki()
        wiki.date_last_active = None
        wiki.date_abandoned = None
        for key, value in kwargs.items():
            setattr(wiki, key, value)
        return wiki

    def test_returns_date_last_active_when_set(self) -> None:
        d = date(2022, 6, 15)
        self.assertEqual(self._wiki(date_last_active=d).effective_date_last_active, d)

    def test_infers_one_day_before_abandoned(self) -> None:
        self.assertEqual(
            self._wiki(date_abandoned=date(2021, 3, 20)).effective_date_last_active,
            date(2021, 3, 20) - timedelta(days=1),
        )

    def test_returns_none_when_both_fields_are_none(self) -> None:
        self.assertIsNone(self._wiki().effective_date_last_active)

    def test_date_last_active_takes_priority_over_abandoned(self) -> None:
        self.assertEqual(
            self._wiki(date_last_active=date(2020, 1, 10), date_abandoned=date(2020, 5, 1)).effective_date_last_active,
            date(2020, 1, 10),
        )
