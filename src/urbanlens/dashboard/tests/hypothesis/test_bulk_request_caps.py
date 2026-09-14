"""A bulk request must name a bounded number of things."""

from __future__ import annotations

import json
import uuid as uuid_module

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.image_gallery import MAX_BULK_IMAGES
from urbanlens.dashboard.controllers.pin_bulk import _MAX_BULK_PINS
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class _Case(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def make_pin(self, index: int) -> Pin:
        location = baker.make(Location, latitude=f"39.{index:06d}", longitude=f"-76.{index:06d}")
        return baker.make(Pin, profile=self.profile, location=location)


class TheMergeEndpointTests(_Case):
    """Each source is re-saved, and each save re-hulls the target's children."""

    def _post(self, count: int):  # noqa: ANN202
        target = self.make_pin(1)
        return self.client.post(
            reverse("pin.bulk_merge"),
            data=json.dumps(
                {"target_uuid": str(target.uuid), "source_uuids": [str(uuid_module.uuid4()) for _ in range(count)]}
            ),
            content_type="application/json",
        )

    def test_more_sources_than_the_cap_is_refused(self) -> None:
        self.assertEqual(self._post(_MAX_BULK_PINS + 1).status_code, 400)

    def test_the_refusal_says_what_the_limit_is(self) -> None:
        self.assertIn(str(_MAX_BULK_PINS), self._post(_MAX_BULK_PINS + 1).content.decode())

    def test_a_request_at_the_cap_is_not_refused_for_being_too_large(self) -> None:
        """Non-vacuity: the cap must not be refusing everything."""
        body = self._post(_MAX_BULK_PINS).content.decode()
        self.assertNotIn("at most", body, "a request exactly at the cap was refused as too large")


class TheGalleryBulkEndpointsTests(_Case):
    """`image_ids` was parsed with no cap, scoped only by the profile."""

    def _pin_post(self, count: int):  # noqa: ANN202
        pin = self.make_pin(2)
        return self.client.post(
            reverse("pin.gallery.bulk", kwargs={"pin_slug": pin.slug}),
            data=json.dumps({"action": "delete", "image_ids": list(range(count))}),
            content_type="application/json",
        )

    def _vault_post(self, count: int):  # noqa: ANN202
        return self.client.post(
            reverse("vault.photos.bulk"),
            data=json.dumps({"action": "delete", "image_ids": list(range(count))}),
            content_type="application/json",
        )

    def test_a_pin_gallery_bulk_over_the_cap_is_refused(self) -> None:
        self.assertEqual(self._pin_post(MAX_BULK_IMAGES + 1).status_code, 400)

    def test_a_vault_bulk_over_the_cap_is_refused(self) -> None:
        self.assertEqual(self._vault_post(MAX_BULK_IMAGES + 1).status_code, 400)

    def test_a_pin_gallery_bulk_at_the_cap_is_allowed(self) -> None:
        self.assertEqual(self._pin_post(MAX_BULK_IMAGES).status_code, 200)

    def test_the_cap_does_not_stop_an_ordinary_selection(self) -> None:
        """Non-vacuity, and the shape a real toolbar sends."""
        pin = self.make_pin(3)
        images = [baker.make(Image, pin=pin, profile=self.profile) for _ in range(3)]
        response = self.client.post(
            reverse("pin.gallery.bulk", kwargs={"pin_slug": pin.slug}),
            data=json.dumps({"action": "delete", "image_ids": [i.pk for i in images]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Image.objects.filter(pk__in=[i.pk for i in images]).exists(), "the ordinary selection was not deleted"
        )
