"""The shared photo lightbox is rendered once per page, not once per gallery refresh."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class PinLightboxOncePerPageTests(TestCase):
    """The pin gallery refreshes with an ``outerHTML`` swap, which inserts everything the response carries.

    A lightbox in that response added another dialog, picker and script on every refresh, and each script added
    another arrow-key listener, so one keypress stepped through several photos.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)

    def test_a_gallery_refresh_carries_no_lightbox(self) -> None:
        response = self.client.get(reverse("pin.gallery", args=[self.pin.slug]), headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('id="photo-gallery"', content)
        self.assertNotIn('id="gallery-lightbox"', content)
        self.assertNotIn('id="lightbox-picker-dialog"', content)

    def test_the_pin_page_carries_exactly_one(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertEqual(content.count('id="gallery-lightbox"'), 1)
        self.assertEqual(content.count('id="lightbox-picker-dialog"'), 1)
        self.assertIn(f'data-cover-photo-url="{reverse("pin.cover_photo", args=[self.pin.slug])}"', content)
