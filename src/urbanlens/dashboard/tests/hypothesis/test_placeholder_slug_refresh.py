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

from datetime import timedelta

from django.contrib.auth.models import User
from django.utils import timezone
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

    def _pin(self, *, slug: str, name: str | None, age: timedelta | None = None) -> Pin:
        # A distinct Location per pin: one root pin per (location, profile) is a
        # database constraint, and Location itself is unique on (latitude,
        # longitude), so both have to vary between calls.
        self._seq = getattr(self, "_seq", 0) + 1
        location = baker.make(Location, latitude=41.73332 + self._seq / 10000, longitude=-73.92794)
        pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None, name=name)
        fields: dict[str, object] = {"slug": slug}
        # `created` is auto_now_add, so it can only be backdated by an UPDATE.
        if age is not None:
            fields["created"] = timezone.now() - age
        Pin.objects.filter(pk=pin.pk).update(**fields)
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
        pin = self._pin(slug="unnamed-location", name="HRSH", age=timedelta(days=30))

        upgrade_placeholder_pin_names()

        pin.refresh_from_db()
        self.assertNotEqual(pin.slug, "unnamed-location")
        self.assertEqual(pin.slug, "hrsh")

    def test_the_sweep_leaves_good_slugs_alone(self) -> None:
        pin = self._pin(slug="hrsh", name="HRSH", age=timedelta(days=30))

        upgrade_placeholder_pin_names()

        pin.refresh_from_db()
        self.assertEqual(pin.slug, "hrsh")

    def test_the_sweep_will_not_reslug_a_pin_somebody_may_be_looking_at(self) -> None:
        """A pin created minutes ago is not the legacy data this sweep is for.

        Its detail page has the old slug baked into every HTMX panel URL it
        rendered, so changing the slug underneath 404s those panels and the
        global `htmx:responseError` handler raises an error toast for each -
        on a pin the user has just created. Found by `tests/integration/`;
        see docs/PROBLEMS.md, 2026-08-23.

        The pin still heals, an hour later, by which point nobody is holding a
        page that was rendered before it.
        """
        fresh = self._pin(slug="unnamed-location", name="HRSH", age=timedelta(minutes=2))

        upgrade_placeholder_pin_names()

        fresh.refresh_from_db()
        self.assertEqual(
            fresh.slug, "unnamed-location", "A pin created two minutes ago had its URL changed underneath it."
        )

    def test_the_age_guard_is_a_delay_not_an_exemption(self) -> None:
        """The same pin, once old enough, is still repaired."""
        pin = self._pin(slug="unnamed-location", name="HRSH", age=timedelta(minutes=2))
        upgrade_placeholder_pin_names()
        pin.refresh_from_db()
        self.assertEqual(pin.slug, "unnamed-location")

        Pin.objects.filter(pk=pin.pk).update(created=timezone.now() - timedelta(days=1))
        upgrade_placeholder_pin_names()

        pin.refresh_from_db()
        self.assertEqual(pin.slug, "hrsh")
