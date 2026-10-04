"""Lists of a pin's child pins cost the same queries for a few children as for many (P296).

Each child read its labels for its icon, and an unnamed one its location's wiki for its name: on dev, the pin page's
detail-pin overlay ran 596 queries for a 517-child campus, its detail-pin list 120 for a 359-child one, and the main
map's Child pins layer 201 for 196 children. The overlay and the layer also read the labels without the viewer's
customizations, so a label recoloured for the map kept its original colour on both.
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


class _ChildPinListCase(TestCase):
    """A campus pin with a child pin, both of which gain unnamed, labelled detail pins with wikis."""

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

    def _unnamed_detail_pin(self) -> Pin:
        self._add_detail_pins(1, self.root)
        return Pin.objects.get(parent_pin=self.root, name="")

    def _count(self, url: str, params: dict[str, str] | None = None) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(url, params or {})
        self.assertEqual(response.status_code, 200)
        return len(captured)

    def _assert_flat(self, url: str, params: dict[str, str] | None = None) -> None:
        self._add_detail_pins(3, self.root)
        self._add_detail_pins(3, self.child)
        few = self._count(url, params)
        self._add_detail_pins(9, self.root)
        self._add_detail_pins(9, self.child)

        self.assertEqual(self._count(url, params), few)


class DetailPinsJsonQueryTests(_ChildPinListCase):
    """The pin page's detail-pin map overlay."""

    def _url(self) -> str:
        return reverse("pin.detail_pins.json", kwargs={"pin_slug": self.root.slug})

    def _payload(self) -> dict[int, dict]:
        return {row["id"]: row for row in self.client.get(self._url()).json()["detail_pins"]}

    def test_the_query_count_does_not_grow_with_the_detail_pins(self) -> None:
        self._assert_flat(self._url())

    def test_nor_with_every_descendant_shown(self) -> None:
        self._assert_flat(self._url(), {"children": "1"})

    def test_an_unnamed_detail_pin_is_named_by_its_wiki_and_drawn_with_its_label(self) -> None:
        detail = self._unnamed_detail_pin()

        row = self._payload()[detail.pk]

        self.assertEqual(row["name"], f"zz-detail-wiki-{detail.pk}")
        self.assertEqual((row["icon"], row["color"]), ("tower", "#111111"))

    def test_the_viewers_own_label_colour_and_icon_are_used(self) -> None:
        detail = self._unnamed_detail_pin()
        LabelCustomization.objects.create(profile=self.profile, label=self.label, icon="castle", color="#abcdef")

        row = self._payload()[detail.pk]

        self.assertEqual((row["icon"], row["color"]), ("castle", "#abcdef"))


class MapChildPinsQueryTests(DetailPinsJsonQueryTests):
    """The main map's Child pins layer, which lists every child pin the viewer owns."""

    def _url(self) -> str:
        return reverse("map.pins.children")

    def _payload(self) -> dict[int, dict]:
        return {row["id"]: row for row in self.client.get(self._url()).json()["pins"]}

    def test_an_unnamed_parent_is_named_by_its_wiki(self) -> None:
        parent = self._pin("", parent_pin=self.root)
        baker.make(Wiki, location=parent.location, name="zz-parent-wiki")
        child = self._pin("Door", parent_pin=parent)

        self.assertEqual(self._payload()[child.pk]["parent_name"], "zz-parent-wiki")


class DetailPinPanelQueryTests(_ChildPinListCase):
    """The pin page's detail-pin list, which shows each child's name and coordinates."""

    def _url(self) -> str:
        return reverse("pin.detail_pins", kwargs={"pin_slug": self.root.slug})

    def test_the_query_count_does_not_grow_with_the_detail_pins(self) -> None:
        self._assert_flat(self._url())

    def test_an_unnamed_detail_pin_is_named_by_its_wiki(self) -> None:
        detail = self._unnamed_detail_pin()

        self.assertContains(self.client.get(self._url()), f"zz-detail-wiki-{detail.pk}")
