"""A pin reference field's choices cost the same queries for 3 pins as for 12.

An unnamed pin is labelled by its location's wiki, read once per pin: on dev, a pin's Custom Fields card with a pin
reference field ran 318 queries, 310 of them that read.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.custom_fields.custom_field_references import capped_reference_choices


class PinReferenceChoiceQueryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def _add_unnamed_pins(self, count: int) -> list[Pin]:
        made = []
        for _ in range(count):
            offset = Pin.objects.count() * 0.0007
            location = baker.make(Location, latitude=42.3 + offset, longitude=-73.4 - offset)
            pin = baker.make(Pin, profile=self.profile, location=location, name="")
            baker.make(Wiki, location=location, name=f"zz-choice-wiki-{pin.pk}")
            made.append(pin)
        return made

    def _queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            capped_reference_choices("pin", self.profile)
        return len(captured)

    def test_the_query_count_does_not_grow_with_the_pins(self) -> None:
        self._add_unnamed_pins(3)
        few = self._queries()
        self._add_unnamed_pins(9)

        self.assertEqual(self._queries(), few)

    def test_an_unnamed_pin_is_labelled_by_its_wiki(self) -> None:
        (pin,) = self._add_unnamed_pins(1)

        self.assertIn((pin.pk, f"zz-choice-wiki-{pin.pk}"), capped_reference_choices("pin", self.profile))
