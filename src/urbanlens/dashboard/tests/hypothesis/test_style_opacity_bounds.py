"""A posted opacity or stroke width is clamped to the range it means, rather than kept as sent or a 500.

The columns are 32-bit ``IntegerField``s with no validator: 150 was stored and rendered as-is, and a
30-digit value overflowed the column on save.
"""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki

_HUGE = int("9" * 30)
_POLYGON = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 0]]]}


class _OwnPinCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self.pin = baker.make(
            Pin, profile=self.profile, location=Location.objects.create(latitude=40.0, longitude=-74.0)
        )

    def _post_json(self, url: str, body: dict):
        return self.client.post(url, data=json.dumps(body), content_type="application/json")


class MarkupOpacityTests(_OwnPinCase):
    def _create(self, **style) -> PinMarkup:
        response = self._post_json(
            reverse("pin.markup", kwargs={"pin_slug": self.pin.slug}),
            {"markup_type": "polygon", "geometry": _POLYGON, **style},
        )
        self.assertLess(response.status_code, 400)
        return PinMarkup.objects.get(profile=self.profile)

    def test_creating_with_an_opacity_over_100(self) -> None:
        item = self._create(fill_opacity=150, border_opacity=-20)

        self.assertEqual((item.fill_opacity, item.border_opacity), (100, 0))

    def test_editing_with_an_opacity_beyond_the_column(self) -> None:
        item = self._create()

        response = self._post_json(
            reverse("pin.markup.edit", kwargs={"pin_slug": self.pin.slug, "markup_uuid": item.uuid}),
            {"fill_opacity": _HUGE, "border_opacity": 55},
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual((item.fill_opacity, item.border_opacity), (100, 55))

    def test_stroke_width_is_bounded_like_an_import(self) -> None:
        # Import clamps it to [1, 200]; the drawing endpoints stored whatever arrived.
        item = self._create(stroke_width=0)
        self.assertEqual(item.stroke_width, 1)

        response = self._post_json(
            reverse("pin.markup.edit", kwargs={"pin_slug": self.pin.slug, "markup_uuid": item.uuid}),
            {"stroke_width": _HUGE},
        )

        self.assertEqual(response.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.stroke_width, 200)


class DetailPinOpacityTests(_OwnPinCase):
    def test_creating_with_an_opacity_beyond_the_column(self) -> None:
        response = self._post_json(
            reverse("pin.detail_pins", kwargs={"pin_slug": self.pin.slug}),
            {"latitude": 40.0003, "longitude": -74.0003, "bg_opacity": _HUGE, "border_opacity": 101},
        )

        self.assertLess(response.status_code, 500)
        child = Pin.objects.get(parent_pin=self.pin)
        self.assertEqual((child.detail_bg_opacity, child.detail_border_opacity), (100, 100))

    def test_editing_with_a_negative_opacity(self) -> None:
        child = baker.make(Pin, profile=self.profile, parent_pin=self.pin, location=baker.make(Location))

        response = self._post_json(
            reverse("pin.detail_pin.edit", kwargs={"pin_slug": self.pin.slug, "detail_pin_uuid": child.uuid}),
            {"bg_opacity": -5, "border_opacity": _HUGE},
        )

        self.assertEqual(response.status_code, 200)
        child.refresh_from_db()
        self.assertEqual((child.detail_bg_opacity, child.detail_border_opacity), (0, 100))


class ChildWikiOpacityTests(TestCase):
    def test_editing_with_an_opacity_beyond_the_column(self) -> None:
        baker.make("auth.User")  # the first user is auto-promoted to site admin
        user = baker.make("auth.User")
        self.client.force_login(user)
        location = Location.objects.create(latitude=40.0, longitude=-74.0)
        parent = baker.make_recipe("dashboard.wiki", location=location)
        baker.make_recipe("dashboard.pin", profile=user.profile, location=location)
        child = baker.make_recipe(
            "dashboard.wiki", parent_wiki=parent, location=Location.objects.create(latitude=40.001, longitude=-74.001)
        )

        response = self.client.post(
            reverse("location.wiki.detail_pin.edit", args=[location.slug, child.uuid]),
            data=json.dumps({"bg_opacity": _HUGE, "border_opacity": 250}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        child = Wiki.objects.get(pk=child.pk)
        self.assertEqual((child.detail_bg_opacity, child.detail_border_opacity), (100, 100))
