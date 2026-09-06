"""The shared page hero renders on both of the pages that use it.

`_page_hero.html` builds a localStorage key for the saved cover-image position
from whichever of `pin` or `wiki` the current page has. Written as
``pin.slug|default:wiki.location.slug``, that named a variable the pin page does
not have - and a filter *argument* is resolved with no failure tolerance, so it
raised straight out of the render rather than falling through.

The block only renders when `hero_image_url` is passed, and the Private Pin page
is currently the only include site that passes it, so reaching this took a pin
that actually had a cover photo - which is why it went unnoticed.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.images import JPEG_BYTES
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


def _photo(profile: Profile, **kwargs) -> Image:
    return baker.make(
        Image, profile=profile, image=SimpleUploadedFile("cover.jpg", JPEG_BYTES, content_type="image/jpeg"), **kwargs
    )


class PinCoverHeroTests(TestCase):
    """GET /dashboard/map/pin/<slug>/ for a pin that has a cover photo."""

    def setUp(self) -> None:
        baker.make("auth.User")  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def test_the_page_renders_with_a_cover_photo(self) -> None:
        self.pin.cover_photo = _photo(self.profile, pin=self.pin)
        self.pin.save(update_fields=["cover_photo"])

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)

    def test_the_saved_position_is_keyed_to_this_pin(self) -> None:
        self.pin.cover_photo = _photo(self.profile, pin=self.pin)
        self.pin.save(update_fields=["cover_photo"])

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))

        self.assertContains(response, f"'_' + '{self.pin.slug}'")


class OtherHeroPagesTests(TestCase):
    """The same partial on a page that passes no cover image at all.

    The Private Pin page is currently the only include site that passes
    `hero_image_url`, so nothing else renders the block this is about - which is
    how a 500 in it went unnoticed. Pinned here so a page that starts passing
    one is not the way that is rediscovered.
    """

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_the_wiki_page_renders_its_hero_without_the_cover_script(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.location.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="wiki-hero"')
        self.assertNotContains(response, "ul_cover_hero_")
