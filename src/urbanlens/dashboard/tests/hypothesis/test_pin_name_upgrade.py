"""tasks.upgrade_placeholder_pin_names - the periodic sweep that clears a pin's stored
placeholder name once its location has resolved to a meaningful one.

Covers the reported gap: legacy pins from earlier ingestion pipelines stored a literal
placeholder string ("Dropped Pin", raw coordinates, "Unnamed Location") directly on
Pin.name with name_is_user_provided=False, so Pin.effective_name never picked up a
better name even after the Location later resolved a meaningful official_name.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki


class WithPlaceholderNamesQuerySetTests(TestCase):
    """PinQuerySet.with_placeholder_names - the SQL-cheap half of the candidate filter."""

    def setUp(self) -> None:
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def test_excludes_user_provided_names(self) -> None:
        baker.make(Pin, profile=self.profile, name="Dropped Pin", name_is_user_provided=True)
        self.assertFalse(Pin.objects.with_placeholder_names().exists())

    def test_excludes_pins_with_no_stored_name(self) -> None:
        baker.make(Pin, profile=self.profile, name=None, name_is_user_provided=False)
        baker.make(Pin, profile=self.profile, name="", name_is_user_provided=False)
        self.assertFalse(Pin.objects.with_placeholder_names().exists())

    def test_includes_placeholder_named_pins(self) -> None:
        pin = baker.make(Pin, profile=self.profile, name="Dropped Pin", name_is_user_provided=False)
        self.assertEqual(list(Pin.objects.with_placeholder_names()), [pin])


class UpgradePlaceholderPinNamesTaskTests(TestCase):
    """tasks.upgrade_placeholder_pin_names end-to-end behavior."""

    def setUp(self) -> None:
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def test_clears_placeholder_name_when_location_has_meaningful_name(self) -> None:
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        location = baker.make(Location, official_name="Old Steel Mill", latitude="40.0", longitude="-74.0")
        pin = baker.make(Pin, profile=self.profile, location=location, name="Dropped Pin", name_is_user_provided=False)

        upgraded = upgrade_placeholder_pin_names()

        self.assertEqual(upgraded, 1)
        pin.refresh_from_db()
        self.assertIsNone(pin.name)
        self.assertEqual(pin.effective_name, "Old Steel Mill")

    def test_leaves_pin_alone_when_location_name_still_not_meaningful(self) -> None:
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        location = baker.make(Location, official_name="", latitude="40.0", longitude="-74.0", slug=None)
        pin = baker.make(Pin, profile=self.profile, location=location, name="Dropped Pin", name_is_user_provided=False)

        upgraded = upgrade_placeholder_pin_names()

        self.assertEqual(upgraded, 0)
        pin.refresh_from_db()
        self.assertEqual(pin.name, "Dropped Pin")

    def test_never_touches_a_user_provided_name(self) -> None:
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        location = baker.make(Location, official_name="Old Steel Mill", latitude="40.0", longitude="-74.0")
        pin = baker.make(Pin, profile=self.profile, location=location, name="Dropped Pin", name_is_user_provided=True)

        upgraded = upgrade_placeholder_pin_names()

        self.assertEqual(upgraded, 0)
        pin.refresh_from_db()
        self.assertEqual(pin.name, "Dropped Pin")

    def test_leaves_an_already_meaningful_pin_name_alone(self) -> None:
        """A stored name that's already meaningful isn't a placeholder - nothing to upgrade."""
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        location = baker.make(Location, official_name="Old Steel Mill", latitude="40.0", longitude="-74.0")
        pin = baker.make(Pin, profile=self.profile, location=location, name="My Favorite Ruin", name_is_user_provided=False)

        upgraded = upgrade_placeholder_pin_names()

        self.assertEqual(upgraded, 0)
        pin.refresh_from_db()
        self.assertEqual(pin.name, "My Favorite Ruin")

    def test_prefers_wiki_name_over_official_name(self) -> None:
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        location = baker.make(Location, official_name="123 Main St", latitude="40.0", longitude="-74.0")
        baker.make(Wiki, location=location, name="The Old Steel Mill")
        pin = baker.make(Pin, profile=self.profile, location=location, name="Dropped Pin", name_is_user_provided=False)

        upgraded = upgrade_placeholder_pin_names()

        self.assertEqual(upgraded, 1)
        pin.refresh_from_db()
        self.assertIsNone(pin.name)
        self.assertEqual(pin.effective_name, "The Old Steel Mill")

    def test_respects_batch_size(self) -> None:
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        for i in range(3):
            location = baker.make(Location, official_name="Old Steel Mill", latitude=f"40.{i}", longitude=f"-74.{i}")
            baker.make(Pin, profile=self.profile, location=location, name="Dropped Pin", name_is_user_provided=False)

        upgraded = upgrade_placeholder_pin_names(batch_size=2)

        self.assertEqual(upgraded, 2)
        self.assertEqual(Pin.objects.with_placeholder_names().count(), 1)

    def test_coordinate_name_is_upgraded_too(self) -> None:
        """Raw coordinate strings are a named example in the original request."""
        from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names

        location = baker.make(Location, official_name="Old Steel Mill", latitude="40.0", longitude="-74.0")
        pin = baker.make(Pin, profile=self.profile, location=location, name="40.0, -74.0", name_is_user_provided=False)

        upgraded = upgrade_placeholder_pin_names()

        self.assertEqual(upgraded, 1)
        pin.refresh_from_db()
        self.assertIsNone(pin.name)
