"""`Pin.__str__` must not query, and must stay on one line.

It previously ran `self.labels.filter(kind="status")` and read `effective_name`, which
falls through to `self.location.display_name` - so rendering a pin cost up to two
queries. `__str__` runs on every repr: an admin list of 100 pins paid 100+ times, as did
every log line and error page mentioning a pin. `CLAUDE.md` already forbids `save()`
inside `__str__`; a query is the same class of problem and much easier to miss.

It also returned a five-line string, which renders as a paragraph inside admin select
dropdowns and breaks line-oriented log grepping.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class PinStrTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make(User))
        location = baker.make(Location, latitude=42.65, longitude=-73.75, point=Point(-73.75, 42.65, srid=4326))
        self.pin = baker.make(Pin, profile=self.profile, location=location, name="ZzAudit Str Pin")

    def test_str_issues_no_queries(self) -> None:
        pin = Pin.objects.get(pk=self.pin.pk)

        with self.assertNumQueries(0):
            str(pin)

    def test_str_is_a_single_line(self) -> None:
        self.assertNotIn("\n", str(self.pin))

    def test_str_identifies_the_pin(self) -> None:
        self.assertEqual(str(self.pin), "ZzAudit Str Pin")

    def test_a_pin_without_a_name_still_identifies_itself(self) -> None:
        unnamed = baker.make(Pin, profile=self.profile, name="")

        self.assertEqual(str(unnamed), f"Pin {unnamed.pk}")
