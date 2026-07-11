"""Tests for the Wiki model and the Location/Wiki split.

Covers the core invariants of the community-wiki extraction:
- Wiki holds the community name; Location keeps only address/official_name.
- ``Location.display_name`` / ``Pin.effective_name`` resolve through the wiki.
- ``Wiki.objects.get_or_create_for_location`` is lazy and idempotent.
- Comments are pin-XOR-wiki; community aliases/edits/detail-pins live on Wiki.
"""

from __future__ import annotations

from datetime import date, timedelta

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
