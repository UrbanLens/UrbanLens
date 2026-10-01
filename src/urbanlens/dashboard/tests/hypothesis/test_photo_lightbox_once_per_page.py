"""The shared photo lightbox is rendered once per page, not once per gallery refresh."""

from __future__ import annotations

import re
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

    def test_a_gallery_refresh_replaces_only_the_card(self) -> None:
        """The label slot, bulk bar and drop overlay that follow the card stay where the first load put them."""
        response = self.client.get(reverse("pin.gallery", args=[self.pin.slug]), headers={"HX-Request": "true"})
        card = re.search(r'<div class="photo-gallery card" id="photo-gallery"([^>]*)>', response.content.decode())
        assert card is not None
        self.assertIn('hx-trigger="refreshGallery from:body"', card.group(1))
        self.assertIn('hx-select="#photo-gallery"', card.group(1))
        # Without this, the card's own htmx controls would select a card out of their responses too.
        self.assertIn('hx-disinherit="hx-select"', card.group(1))

    def test_the_pin_page_carries_exactly_one(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertEqual(content.count('id="gallery-lightbox"'), 1)
        self.assertEqual(content.count('id="lightbox-picker-dialog"'), 1)
        self.assertIn(f'data-cover-photo-url="{reverse("pin.cover_photo", args=[self.pin.slug])}"', content)
