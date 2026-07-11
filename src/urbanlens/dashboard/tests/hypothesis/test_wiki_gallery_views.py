"""Tests for the location-wiki photo gallery views in image_gallery.py.

Covers the regression where a Location without a Wiki (wikis are opt-in -
see Wiki.objects.get_for_location) caused ``Image.objects.filter(wiki=None)``
to match every wiki-less image site-wide instead of scoping to the location.
"""
from __future__ import annotations

import tempfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _make_image(**kwargs) -> Image:
    return Image.objects.create(image=SimpleUploadedFile("photo.jpg", b"fake image bytes", content_type="image/jpeg"), **kwargs)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class WikiGalleryNoWikiTests(TestCase):
    """Locations without a Wiki must not expose or accept gallery images."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        # A photo attached to an unrelated pin, with no wiki association -
        # this is what the old `wiki=None` filter would have leaked.
        other_pin = baker.make(Pin, profile=self.profile)
        self.unrelated_image = _make_image(pin=other_pin, wiki=None, profile=self.profile)

    def test_gallery_panel_404s_without_wiki(self) -> None:
        response = self.client.get(reverse("location.wiki.gallery", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_gallery_json_404s_without_wiki(self) -> None:
        response = self.client.get(reverse("location.wiki.gallery.json", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_upload_404s_without_wiki(self) -> None:
        response = self.client.post(reverse("location.wiki.gallery", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class WikiGalleryScopingTests(TestCase):
    """When a wiki exists, its gallery must only show images for that wiki."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        # Wikis are only visible to profiles with a pin at that location.
        baker.make(Pin, profile=self.profile, location=self.location)
        self.own_image = _make_image(wiki=self.wiki, location=self.location, profile=self.profile, latitude="1.0", longitude="2.0")

        other_pin = baker.make(Pin, profile=self.profile)
        self.unrelated_image = _make_image(pin=other_pin, wiki=None, profile=self.profile, latitude="3.0", longitude="4.0")

    def test_gallery_panel_excludes_unrelated_wiki_less_images(self) -> None:
        response = self.client.get(reverse("location.wiki.gallery", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200)
        images = list(response.context["images"])
        self.assertIn(self.own_image, images)
        self.assertNotIn(self.unrelated_image, images)

    def test_gallery_json_excludes_unrelated_wiki_less_images(self) -> None:
        response = self.client.get(reverse("location.wiki.gallery.json", args=[self.location.slug]))
        ids = [img["id"] for img in response.json()["images"]]
        self.assertIn(self.own_image.pk, ids)
        self.assertNotIn(self.unrelated_image.pk, ids)
