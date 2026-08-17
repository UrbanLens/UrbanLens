"""A pin named after the fact must stop being addressed as `unnamed-location`.

Reported from staging: a pin named "HRSH", carrying three aliases, was still at
`/unnamed-location`. Slugs are generated once, when the row is first saved, and
nothing ever revisits them - so a pin created before anything knew what it was
keeps the placeholder in its URL permanently, however well-named it later
becomes.

The rule is deliberately narrow, because slugs are addresses and changing one
breaks whatever links to it: a slug is replaced only when it *still reads as a
placeholder*, judged by the same `is_meaningful_name` the naming service uses on
any other name. A slug derived from a real name is never touched, even if the pin
is renamed afterwards.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.tasks import upgrade_placeholder_pin_names


class PlaceholderSlugTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile

    def _pin(self, *, slug: str, name: str | None) -> Pin:
        # A distinct Location per pin: one root pin per (location, profile) is a
        # database constraint, and Location itself is unique on (latitude,
        # longitude), so both have to vary between calls.
        self._seq = getattr(self, "_seq", 0) + 1
        location = baker.make(Location, latitude=41.73332 + self._seq / 10000, longitude=-73.92794)
        pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name=name)
        Pin.objects.filter(pk=pin.pk).update(slug=slug)
        pin.refresh_from_db()
        return pin

    def test_a_placeholder_slug_is_recognised(self) -> None:
        self.assertTrue(self._pin(slug="unnamed-location", name="HRSH").slug_is_placeholder())
        self.assertTrue(self._pin(slug="dropped-pin", name="HRSH").slug_is_placeholder())

    def test_a_real_slug_is_not(self) -> None:
        self.assertFalse(self._pin(slug="hrsh", name="HRSH").slug_is_placeholder())
        self.assertFalse(self._pin(slug="hudson-river-state-hospital", name="HRSH").slug_is_placeholder())

    def test_a_named_pin_gets_a_real_slug(self) -> None:
        pin = self._pin(slug="unnamed-location", name="HRSH")

        self.assertTrue(pin.refresh_placeholder_slug())

        pin.refresh_from_db()
        self.assertEqual(pin.slug, "hrsh")

    def test_an_unnamed_pin_keeps_its_placeholder(self) -> None:
        """Nothing better is available yet, so there is nothing to replace it with."""
        pin = self._pin(slug="unnamed-location", name=None)

        self.assertFalse(pin.refresh_placeholder_slug())

        pin.refresh_from_db()
        self.assertEqual(pin.slug, "unnamed-location")

    def test_a_real_slug_is_never_rewritten(self) -> None:
        """Slugs are addresses: renaming a pin must not break its existing links."""
        pin = self._pin(slug="hudson-river-state-hospital", name="HRSH")

        self.assertFalse(pin.refresh_placeholder_slug())

        pin.refresh_from_db()
        self.assertEqual(pin.slug, "hudson-river-state-hospital")

    def test_the_hourly_sweep_repairs_existing_pins(self) -> None:
        """The reported pin was named long ago; it has to heal without being edited."""
        pin = self._pin(slug="unnamed-location", name="HRSH")

        upgrade_placeholder_pin_names()

        pin.refresh_from_db()
        self.assertNotEqual(pin.slug, "unnamed-location")
        self.assertEqual(pin.slug, "hrsh")

    def test_the_sweep_leaves_good_slugs_alone(self) -> None:
        pin = self._pin(slug="hrsh", name="HRSH")

        upgrade_placeholder_pin_names()

        pin.refresh_from_db()
        self.assertEqual(pin.slug, "hrsh")
