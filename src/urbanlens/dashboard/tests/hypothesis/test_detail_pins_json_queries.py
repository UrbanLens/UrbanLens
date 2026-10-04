"""The pin page's detail-pin map overlay costs the same queries for 3 detail pins as for 12.

Dev's campus pin with ~300 detail pins answered in 596 queries: each pin read its labels for its icon, and an unnamed
pin read its location's wiki for its name. The labels were also read without the viewer's customizations, so a
label recoloured on the map kept its original colour here.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.meta import KIND_TAG
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki


class DetailPinsJsonQueryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.label = baker.make(
            Label, kind=KIND_TAG, profile=self.profile, name="zz-detail-tower", icon="tower", color="#111111"
        )
        self.root = self._pin("Campus")
        self.child = self._pin("Boiler House", parent_pin=self.root)

    def _pin(self, name: str, **kwargs) -> Pin:
        count = Pin.objects.count() + 1
        location = baker.make(Location, latitude=41.7 + count * 0.0007, longitude=-73.9 - count * 0.0007)
        return baker.make(Pin, profile=self.profile, location=location, name=name, icon=None, custom_icon="", **kwargs)

    def _add_detail_pins(self, count: int, parent: Pin) -> None:
        for _ in range(count):
            pin = self._pin("", parent_pin=parent)
            baker.make(Wiki, location=pin.location, name=f"zz-detail-wiki-{pin.pk}")
            pin.labels.add(self.label)

    def _queries(self, *, children: bool) -> int:
        url = reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug})
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, {"children": "1"} if children else {})
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def test_the_query_count_does_not_grow_with_the_detail_pins(self) -> None:
        for children in (False, True):
            with self.subTest(children=children):
                self._add_detail_pins(3, self.root)
                self._add_detail_pins(3, self.child)
                few = self._queries(children=children)
                self._add_detail_pins(9, self.root)
                self._add_detail_pins(9, self.child)
                many = self._queries(children=children)

                self.assertEqual(many, few)

    def test_an_unnamed_detail_pin_is_named_by_its_wiki_and_drawn_with_its_label(self) -> None:
        self._add_detail_pins(1, self.root)
        detail = Pin.objects.get(parent_pin=self.root, name="")

        payload = {
            row["id"]: row
            for row in self.client.get(reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug})).json()[
                "detail_pins"
            ]
        }

        self.assertEqual(payload[detail.pk]["name"], f"zz-detail-wiki-{detail.pk}")
        self.assertEqual((payload[detail.pk]["icon"], payload[detail.pk]["color"]), ("tower", "#111111"))

    def test_the_viewers_own_label_colour_and_icon_are_used(self) -> None:
        self._add_detail_pins(1, self.root)
        detail = Pin.objects.get(parent_pin=self.root, name="")
        LabelCustomization.objects.create(profile=self.profile, label=self.label, icon="castle", color="#abcdef")

        payload = {
            row["id"]: row
            for row in self.client.get(reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug})).json()[
                "detail_pins"
            ]
        }

        self.assertEqual((payload[detail.pk]["icon"], payload[detail.pk]["color"]), ("castle", "#abcdef"))


class MapChildPinsQueryTests(DetailPinsJsonQueryTests):
    """The main map's Child pins layer: 201 queries for e2e-primary's 196 child pins on dev, and the same label colours."""

    def _queries(self, *, children: bool = True) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("map.pins.children"))
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def _payload(self) -> dict[int, dict]:
        return {row["id"]: row for row in self.client.get(reverse("map.pins.children")).json()["pins"]}

    def test_an_unnamed_detail_pin_is_named_by_its_wiki_and_drawn_with_its_label(self) -> None:
        self._add_detail_pins(1, self.root)
        detail = Pin.objects.get(parent_pin=self.root, name="")

        row = self._payload()[detail.pk]

        self.assertEqual(row["name"], f"zz-detail-wiki-{detail.pk}")
        self.assertEqual((row["icon"], row["color"]), ("tower", "#111111"))

    def test_the_viewers_own_label_colour_and_icon_are_used(self) -> None:
        self._add_detail_pins(1, self.root)
        detail = Pin.objects.get(parent_pin=self.root, name="")
        LabelCustomization.objects.create(profile=self.profile, label=self.label, icon="castle", color="#abcdef")

        row = self._payload()[detail.pk]

        self.assertEqual((row["icon"], row["color"]), ("castle", "#abcdef"))

    def test_an_unnamed_parent_is_named_by_its_wiki(self) -> None:
        parent = self._pin("", parent_pin=self.root)
        baker.make(Wiki, location=parent.location, name="zz-parent-wiki")
        child = self._pin("Door", parent_pin=parent)

        self.assertEqual(self._payload()[child.pk]["parent_name"], "zz-parent-wiki")
