"""A child pin's photos in the parent's gallery are managed from the child pin, bulk selection included.

The bulk endpoint only acts on the pin it is called for, so a child-pin photo in the selection was skipped
while the gallery removed its tile as though it had been deleted.
"""

from __future__ import annotations

import json
import re

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin


class ChildPinPhotoSelectionTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        profile = self.user.profile
        location = Location.objects.create(latitude=42.5, longitude=-73.5)
        self.pin = Pin.objects.create(profile=profile, location=location, name="Parent")
        child = Pin.objects.create(profile=profile, location=location, name="Child", parent_pin=self.pin)
        self.own = baker.make(Image, pin=self.pin, location=location, profile=profile)
        self.childs = baker.make(Image, pin=child, location=location, profile=profile)

    def _tile(self, image: Image) -> str:
        response = self.client.get(reverse("pin.gallery", args=[self.pin.slug]) + "?children=1")
        self.assertEqual(response.status_code, 200)
        found = re.search(
            rf'<li class="gallery-item" id="gallery-item-{image.pk}".*?</li>', response.content.decode(), re.DOTALL
        )
        assert found is not None, "the photo is not in the gallery"
        return found.group(0)

    def test_the_pin_s_own_photo_is_selectable(self) -> None:
        self.assertIn('data-gallery-action="select"', self._tile(self.own))

    def test_a_child_pin_s_photo_is_not(self) -> None:
        self.assertNotIn('data-gallery-action="select"', self._tile(self.childs))

    def test_the_bulk_response_names_only_the_photos_it_acted_on(self) -> None:
        response = self.client.post(
            reverse("pin.gallery.bulk", args=[self.pin.slug]),
            data=json.dumps({"action": "delete", "image_ids": [self.own.pk, self.childs.pk]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["image_ids"], [self.own.pk])
        self.assertTrue(Image.objects.filter(pk=self.childs.pk).exists())
