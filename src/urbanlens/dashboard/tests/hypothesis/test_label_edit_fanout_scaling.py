"""Editing one label must not cost work proportional to the pins carrying it.

A label's icon and colour appear on every pin that carries it and has none of
its own, so editing a label really does change what a lot of pins draw. What
P102 was about is *where* that work happened: the receiver walked every carrying
pin and rebuilt its cached payload, each one re-fetching the profile, re-fetching
the pin and constructing a fresh Redis client - inside the editing user's own
request. A user with 20,000 pins who recoloured a label they used everywhere
spent tens of thousands of round trips before their request returned.

That mechanism is gone: there is no per-pin cache to rewrite, and the edit is one
`UPDATE` moving the carrying pins' `updated`. This holds the shape - the cost of
editing a label must not track how many pins carry it - because the obvious way
to make some later feature work is a loop over exactly those pins.

Counted, not timed: a wall-clock assertion on a shared host is a flaky test that
gets deleted, and a statement count is exact and machine-independent.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

#: Two sizes far enough apart that per-pin work would be unmistakable.
SMALL = 3
LARGE = 12

#: Extra statements permitted per additional carrying pin. Flat means zero; this
#: is the slack that keeps the assertion about slope rather than constants.
MAX_QUERIES_PER_PIN = 0.5

#: Statements a label edit may run regardless of how many pins carry it: the
#: label's own UPDATE, the bump of the carrying pins, and the lookups around
#: them. Generous on purpose - a loose constant still fails a per-pin loop.
MAX_CONSTANT_QUERIES = 15


class LabelEditCostIsFlatTests(TestCase):
    """The cost of a label edit must not track how many pins carry the label."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.label = baker.make(Label, kind="tag", name="Fanout Probe")
        self._seeded = 0

    def seed_pins(self, count: int) -> None:
        """Create *count* more pins for this profile, all carrying the probe label.

        Args:
            count: How many to add.
        """
        for _ in range(count):
            self._seeded += 1
            # Locations are unique on (latitude, longitude).
            location = baker.make(
                Location,
                latitude=f"41.{self._seeded:06d}",
                longitude=f"-73.{self._seeded:06d}",
                official_name="Fanout Place",
            )
            pin = baker.make(Pin, profile=self.profile, location=location)
            pin.labels.set([self.label])

    def edit_the_label(self) -> int:
        """Recolour the label, and report how many statements it took.

        Counted across the commit callbacks too, since a receiver that deferred
        its work to `on_commit` would otherwise look free.

        Returns:
            Statements executed.
        """
        self.label.color = f"#{self._seeded:06x}"
        with CaptureQueriesContext(connection) as captured, self.captureOnCommitCallbacks(execute=True):
            self.label.save(update_fields=["color"])
        return len(captured.captured_queries)

    def test_it_does_not_query_per_pin_carrying_the_label(self) -> None:
        self.seed_pins(SMALL)
        small = self.edit_the_label()

        self.seed_pins(LARGE - SMALL)
        large = self.edit_the_label()

        per_pin = (large - small) / (LARGE - SMALL)
        self.assertLessEqual(
            per_pin,
            MAX_QUERIES_PER_PIN,
            f"editing one label ran {small} statements against {SMALL} carrying pins and {large} against "
            f"{LARGE} - {per_pin:.1f} extra per pin, inside the user's own request. See this module's docstring.",
        )

    def test_the_constant_cost_is_small(self) -> None:
        """Flatness is necessary but not sufficient - the constant matters too."""
        self.seed_pins(SMALL)

        queries = self.edit_the_label()

        self.assertLessEqual(
            queries,
            MAX_CONSTANT_QUERIES,
            f"editing one label ran {queries} statements against only {SMALL} carrying pins",
        )

    def test_the_edit_reaches_the_carrying_pins(self) -> None:
        """Guard: a flat cost is trivially achieved by doing nothing at all."""
        self.seed_pins(SMALL)
        before = list(Pin.objects.filter(profile=self.profile).values_list("updated", flat=True).order_by("pk"))

        self.edit_the_label()

        after = list(Pin.objects.filter(profile=self.profile).values_list("updated", flat=True).order_by("pk"))
        self.assertTrue(all(new > old for new, old in zip(after, before, strict=True)), "no carrying pin was touched")
