"""Markup geometry is whatever the client posts, and the reader returns all of it.

Two halves of N21 H39, and they compound. Nothing caps how many coordinate pairs
one markup item carries - `geometry` is a JSONField written from the request body
after a type check and nothing else - and `MarkupJsonView` returns every item in a
whole pin/wiki subtree in one response. On a community wiki that markup is read by
everyone who opens the page, so one person's drawing sets what every later viewer
downloads.

Refused at the door for geometry, capped with a marker for the listing: a shape
with a million points is not a shape somebody drew, while a subtree that has
genuinely grown past the ceiling should still render what fits and say so.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

POINTS_SETTING = "MARKUP_MAX_GEOMETRY_POINTS"
ITEMS_SETTING = "MARKUP_MAX_ITEMS_PER_RESPONSE"


class _MarkupCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location))

    def _ring(self, points: int) -> list[list[float]]:
        ring = [[-74.0 + index * 0.0001, 40.0] for index in range(points)]
        return [*ring, ring[0]]

    def _create(self, geometry: dict, markup_type: str = "polygon"):
        return self.client.post(
            reverse("pin.markup", kwargs={"pin_slug": self.pin.slug}),
            data=json.dumps({"markup_type": markup_type, "geometry": geometry}),
            content_type="application/json",
        )

    def _read(self):
        return self.client.get(reverse("pin.markup.json", kwargs={"pin_slug": self.pin.slug}))


class TheSettingsExistTests(_MarkupCase):
    def test_each_ceiling_is_a_real_setting(self) -> None:
        for name in (POINTS_SETTING, ITEMS_SETTING):
            with self.subTest(name):
                self.assertTrue(hasattr(settings, name), f"nothing reads {name}")


class OneShapeCannotBeUnboundedTests(_MarkupCase):
    @override_settings(**{POINTS_SETTING: 10})
    def test_a_geometry_past_the_ceiling_is_refused(self) -> None:
        response = self._create({"type": "Polygon", "coordinates": [self._ring(200)]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PinMarkup.objects.count(), 0, "the oversized shape was stored anyway")

    @override_settings(**{POINTS_SETTING: 10})
    def test_an_ordinary_shape_is_still_accepted(self) -> None:
        """The half that stops the test above passing against a view that refuses everything."""
        response = self._create({"type": "Polygon", "coordinates": [self._ring(4)]})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(PinMarkup.objects.count(), 1)

    @override_settings(**{POINTS_SETTING: 10})
    def test_the_update_path_is_capped_too(self) -> None:
        """Create and update take the same body; capping one door only moves the problem."""
        self.assertEqual(self._create({"type": "Polygon", "coordinates": [self._ring(4)]}).status_code, 200)
        item = PinMarkup.objects.get()

        response = self.client.post(
            reverse("pin.markup.edit", kwargs={"pin_slug": self.pin.slug, "markup_uuid": str(item.uuid)}),
            data=json.dumps({"geometry": {"type": "Polygon", "coordinates": [self._ring(200)]}}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertLessEqual(len(item.geometry["coordinates"][0]), 10)


class TheListingIsCappedTests(_MarkupCase):
    def _seed(self, count: int) -> None:
        for _ in range(count):
            baker.make(
                PinMarkup,
                profile=self.profile,
                parent_pin=self.pin,
                markup_type="line",
                geometry={"type": "LineString", "coordinates": [[-74.0, 40.0], [-74.1, 40.1]]},
            )

    @override_settings(**{ITEMS_SETTING: 5})
    def test_a_large_subtree_is_cut_to_the_ceiling(self) -> None:
        self._seed(20)

        body = self._read().json()

        self.assertEqual(len(body["markup_items"]), 5)

    @override_settings(**{ITEMS_SETTING: 5})
    def test_a_cut_response_says_so(self) -> None:
        """A silent cut reads as "this pin has five drawings", which is a lie."""
        self._seed(20)

        self.assertTrue(self._read().json()["truncated"])

    @override_settings(**{ITEMS_SETTING: 5})
    def test_an_ordinary_subtree_is_not_marked_truncated(self) -> None:
        """The anti-vacuity half: always-true would pass the test above."""
        self._seed(3)

        body = self._read().json()

        self.assertEqual(len(body["markup_items"]), 3)
        self.assertFalse(body["truncated"])


class AMapCannotOutgrowItsReaderTests(_MarkupCase):
    """Items are created one at a time, and nothing counted how many there were.

    The listing ceiling alone leaves a gap with teeth on a standalone map: it
    can pass the ceiling one item at a time, and a snapshot round-trip - the
    composer prefill, or `clone_markup_map` - then writes back only what it
    could read and deletes the rest. Refused at the create door for maps, which
    makes the round-trip lossless.

    Deliberately not applied to pin or wiki markup: neither is ever written
    back from a snapshot, so neither can lose anything - and a ceiling on a
    community wiki would let one person use up a surface everybody draws on,
    which is the harm this whole effort exists to remove.
    """

    def setUp(self) -> None:
        super().setUp()
        self.markup_map = baker.make(
            MarkupMap, profile=self.profile, center_latitude=40.0, center_longitude=-74.0, zoom=13
        )

    def _line(self) -> dict:
        return {"type": "LineString", "coordinates": [[-74.0, 40.0], [-74.1, 40.1]]}

    def _create_on_map(self):
        return self.client.post(
            reverse("markup_map.markup", kwargs={"map_uuid": str(self.markup_map.uuid)}),
            data=json.dumps({"markup_type": "line", "geometry": self._line()}),
            content_type="application/json",
        )

    def _seed_map(self, count: int) -> None:
        for _ in range(count):
            assert self._create_on_map().status_code == 200  # nosec B101

    @override_settings(**{ITEMS_SETTING: 3})
    def test_a_create_past_the_ceiling_is_refused(self) -> None:
        self._seed_map(3)

        response = self._create_on_map()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PinMarkup.objects.count(), 3, "the item over the ceiling was stored anyway")

    @override_settings(**{ITEMS_SETTING: 3})
    def test_creates_under_the_ceiling_still_work(self) -> None:
        """The half that stops the test above passing against a view that refuses everything."""
        self._seed_map(2)

        self.assertEqual(PinMarkup.objects.count(), 2)

    @override_settings(**{ITEMS_SETTING: 3})
    def test_the_ceiling_is_per_map_not_site_wide(self) -> None:
        """A full map must not stop anyone from drawing on a different one."""
        self._seed_map(3)
        other = baker.make(MarkupMap, profile=self.profile, center_latitude=40.0, center_longitude=-74.0, zoom=13)

        response = self.client.post(
            reverse("markup_map.markup", kwargs={"map_uuid": str(other.uuid)}),
            data=json.dumps({"markup_type": "line", "geometry": self._line()}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)

    @override_settings(**{ITEMS_SETTING: 3})
    def test_a_shared_wiki_surface_is_not_capped_at_the_create_door(self) -> None:
        """One person must not be able to use up a surface everybody draws on."""
        for _ in range(5):
            response = self._create(
                {"type": "LineString", "coordinates": [[-74.0, 40.0], [-74.1, 40.1]]}, markup_type="line"
            )
            self.assertEqual(response.status_code, 200, response.content)

        self.assertEqual(PinMarkup.objects.count(), 5)
