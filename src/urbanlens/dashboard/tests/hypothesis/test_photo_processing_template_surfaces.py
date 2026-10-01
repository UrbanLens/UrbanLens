"""Server-rendered photo surfaces draw a pending upload as the shared placeholder, never an empty <img> (P142)."""

from __future__ import annotations

import re

from django.contrib.auth.models import User
from django.template.loader import render_to_string
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.pin_suggestions.model import PinSuggestion, PinSuggestionOrigin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.models.wiki.model import Wiki

#: A page's own lightbox <img> starts empty by design; a photo tile never should.
_EMPTY_IMG = re.compile(r'<img(?![^>]*lightbox)[^>]*\bsrc=""')
_RAW = "pin_images/rawtpl142/upload-tpl142.jpg"


class _Owner(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)

    def pending(self, **fields) -> Image:
        return baker.make(Image, profile=self.profile, image=_RAW, pending_scan=True, **fields)

    def assert_polled_placeholder(self, html: str, image: Image) -> None:
        self.assertIsNone(_EMPTY_IMG.search(html), "an empty <img> is a broken image")
        self.assertNotIn("upload-tpl142", html)
        self.assertIn("media-processing", html)
        self.assertIn(f'data-id="{image.pk}"', html)
        self.assertIn("data-processing-auto", html)
        self.assertIn(f'data-processing-url="{reverse("vault.photos.processing")}"', html)

    def get(self, url: str) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class OwnerSurfaceTests(_Owner):
    def test_profile_photo_strip(self) -> None:
        image = self.pending(wiki=baker.make(Wiki, location=self.location))

        self.assert_polled_placeholder(self.get(reverse("profile.view")), image)

    def test_pin_share_dialog(self) -> None:
        Friendship.objects.create(
            from_profile=self.profile,
            to_profile=baker.make(User).profile,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        image = self.pending(pin=self.pin)

        self.assert_polled_placeholder(self.get(reverse("pin.share.dialog", args=[self.pin.slug])), image)

    def test_wiki_share_dialog(self) -> None:
        baker.make(Wiki, location=self.location)
        image = self.pending(pin=self.pin)

        self.assert_polled_placeholder(self.get(reverse("pin.wiki.share", args=[self.pin.slug])), image)

    def test_visit_edit_form(self) -> None:
        visit = baker.make(PinVisit, pin=self.pin)
        image = self.pending(pin=self.pin, visit=visit)

        self.assert_polled_placeholder(self.get(reverse("pin.visit.edit", args=[self.pin.slug, visit.pk])), image)

    def test_visit_history(self) -> None:
        visit = baker.make(PinVisit, pin=self.pin)
        image = self.pending(pin=self.pin, visit=visit)

        self.assert_polled_placeholder(self.get(reverse("pin.visits", args=[self.pin.slug])), image)

    def test_pin_suggestion_card(self) -> None:
        suggestion = baker.make(PinSuggestion, profile=self.profile, origin=PinSuggestionOrigin.LOCAL_SCAN)
        image = self.pending(pin_suggestion=suggestion)

        html = render_to_string("dashboard/partials/memories/_pin_suggestion_card.html", {"suggestion": suggestion})

        self.assert_polled_placeholder(html, image)

    def test_wiki_cover_hero_names_no_pending_cover(self) -> None:
        wiki = baker.make(Wiki, location=self.location)
        cover = self.pending(wiki=wiki)
        Wiki.objects.filter(pk=wiki.pk).update(cover_photo=cover)

        html = self.get(reverse("location.wiki", args=[self.location.slug]))

        self.assertNotIn("background-image:url('')", html)
        self.assertNotIn("upload-tpl142", html)


class ShareRecipientTests(_Owner):
    """The recipient cannot fetch a pending photo nor poll for it, so theirs is a still placeholder."""

    def test_share_detail(self) -> None:
        recipient = baker.make(User)
        share = baker.make(
            PinShare, pin=self.pin, location=self.location, from_profile=self.profile, to_profile=recipient.profile
        )
        image = self.pending(pin=self.pin)
        share.images.set([image])
        self.client.force_login(recipient)

        html = self.get(reverse("pin.share.detail", args=[share.pk]))

        self.assertIsNone(_EMPTY_IMG.search(html))
        self.assertNotIn("upload-tpl142", html)
        self.assertIn("media-processing", html)
        self.assertNotIn("data-processing-auto", html)
