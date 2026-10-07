"""The pin page asks each panel source whether it applies, and each asked for the pin's search names again.

On dev a pin page ran 60 queries, 22 of them the same two alias reads, once per Media gallery source.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.aliases.model import AliasType
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.pins.search_names import names_remembered, search_names
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin


class PinPageReadsNamesOnceTests(RedataConfiguredMixin, TestCase):
    """REData configured, as CI's placeholders and a developer's `.env` both have it: without it no gallery source
    applies, so the page reads no names at all and the count says nothing."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        location = baker.make(Location, latitude=41.733, longitude=-73.928, google_place=None)
        self.wiki = baker.make(Wiki, location=location, name="Hudson River State Hospital")
        self.pin = baker.make(Pin, profile=self.user.profile, location=location, name="HRSH", slug="zz-names-once")
        self.pin.aliases.create(name="Old Asylum", kind=AliasType.ALTERNATE)

    def test_the_page_reads_the_pins_aliases_once(self) -> None:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("pin.details", kwargs={"pin_slug": self.pin.slug}))

        self.assertEqual(response.status_code, 200)
        name_reads = [
            query["sql"] for query in captured if 'SELECT "dashboard_pin_aliases"."name" AS "name"' in query["sql"]
        ]
        self.assertEqual(len(name_reads), 1, name_reads)

    def test_names_are_remembered_only_inside_the_block(self) -> None:
        with names_remembered():
            first = search_names(self.pin)
            with CaptureQueriesContext(connection) as inside:
                self.assertEqual(search_names(Pin.objects.get(pk=self.pin.pk)), first)
        self.pin.aliases.create(name="Kirkbride", kind=AliasType.ALTERNATE)

        self.assertLessEqual(len(inside), 1, "only the pin itself is read again")
        self.assertIn("kirkbride", search_names(self.pin).custom)
