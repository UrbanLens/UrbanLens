"""The other undo handlers must refuse an impossible restore, not crash on it.

``PinUndoHandler`` establishes the contract: pre-check whatever would make the recreate
fail and raise ``UndoExpiredError``, "since recreating the row would otherwise fail with
an uncaught IntegrityError". The remaining handlers restore rows that carry unique
constraints of their own:

    SavedFilter   unique(profile, name)
    Wiki          location is unique - one wiki per location
    Trip          slug is unique

In each case the same ordinary sequence applies: delete the thing, make another one like
it, then change your mind.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.service import UndoExpiredError, restore_undo_action, stash_for_undo


class SavedFilterUndoConflictTests(TestCase):
    """A saved filter whose name has been taken again cannot be restored."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile

    def test_an_ordinary_undo_restores_the_filter(self):
        saved = SavedFilter.objects.create(profile=self.profile, name="Abandoned mills", criteria={})
        undo_action = stash_for_undo("saved_filter", [saved], self.profile)
        saved.delete()

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "Abandoned mills")

    def test_undo_is_refused_when_the_name_has_been_reused(self):
        saved = SavedFilter.objects.create(profile=self.profile, name="Abandoned mills", criteria={})
        undo_action = stash_for_undo("saved_filter", [saved], self.profile)
        saved.delete()
        SavedFilter.objects.create(profile=self.profile, name="Abandoned mills", criteria={})

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(undo_action)

    def test_another_profile_using_the_name_does_not_block_the_undo(self):
        saved = SavedFilter.objects.create(profile=self.profile, name="Abandoned mills", criteria={})
        undo_action = stash_for_undo("saved_filter", [saved], self.profile)
        saved.delete()
        other: Profile = baker.make("auth.User").profile
        SavedFilter.objects.create(profile=other, name="Abandoned mills", criteria={})

        self.assertEqual(len(restore_undo_action(undo_action)), 1)


class WikiUndoConflictTests(TestCase):
    """A wiki whose location has been given a new one cannot be restored."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile
        self.location = Location.objects.create(latitude=48.1, longitude=-67.4)

    def test_an_ordinary_undo_restores_the_wiki(self):
        wiki = Wiki.objects.create(location=self.location, name="Old mill", created_by=self.profile)
        undo_action = stash_for_undo("wiki", [wiki], self.profile)
        wiki.delete()

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)

    def test_undo_is_refused_when_the_location_has_a_wiki_again(self):
        # Wikis are also created lazily elsewhere (Wiki.objects.get_or_create_for_location),
        # so the location acquiring a new one without an explicit user action is routine.
        wiki = Wiki.objects.create(location=self.location, name="Old mill", created_by=self.profile)
        undo_action = stash_for_undo("wiki", [wiki], self.profile)
        wiki.delete()
        Wiki.objects.create(location=self.location, name="Replacement", created_by=self.profile)

        with self.assertRaises(UndoExpiredError):
            restore_undo_action(undo_action)


class TripUndoConflictTests(TestCase):
    """A trip restore must survive its slug having been taken."""

    def setUp(self):
        super().setUp()
        self.profile: Profile = baker.make("auth.User").profile

    def test_an_ordinary_undo_restores_the_trip(self):
        trip = Trip.objects.create(name="Summer trip", creator=self.profile)
        undo_action = stash_for_undo("trip", [trip], self.profile)
        trip.delete()

        restored = restore_undo_action(undo_action)

        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].name, "Summer trip")

    def test_undo_survives_the_name_being_reused(self):
        trip = Trip.objects.create(name="Summer trip", creator=self.profile)
        undo_action = stash_for_undo("trip", [trip], self.profile)
        trip.delete()
        Trip.objects.create(name="Summer trip", creator=self.profile)

        # Either outcome is acceptable - a fresh slug, or a clean refusal - but not
        # an IntegrityError escaping to the caller.
        try:
            restored = restore_undo_action(undo_action)
        except UndoExpiredError:
            return
        self.assertEqual(len(restored), 1)
        self.assertEqual(Trip.objects.filter(name="Summer trip").count(), 2)
