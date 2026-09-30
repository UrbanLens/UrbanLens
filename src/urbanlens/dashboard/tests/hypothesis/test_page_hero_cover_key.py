"""The shared page hero renders on both of the pages that use it."""

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

        self.assertContains(response, f'data-cover-state-key="ul_cover_hero_pin_{self.pin.slug}"')


class OtherHeroPagesTests(TestCase):
    """The same partial on a page that passes no cover image at all.

    The Private Pin page is currently the only include site that passes `hero_image_url`, so nothing else
    renders the block this is about - which is how a 500 in it went unnoticed."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make("dashboard.Location")
        baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def test_the_wiki_page_renders_its_hero_without_cover_framing(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.location.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="wiki-hero"')
        self.assertNotContains(response, "ul_cover_hero_")


class PinHeroOutOfBandTests(TestCase):
    """The overview partial, loaded on every pin page, swaps its own copy of the hero over the page's.

    That copy was rendered without the other photos, so the hover preview through them never survived page load.
    """

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)
        self.client.force_login(self.user)
        self.cover = _photo(self.profile, pin=self.pin)
        self.other = _photo(self.profile, pin=self.pin)
        self.pin.cover_photo = self.cover
        self.pin.save(update_fields=["cover_photo"])

    def test_the_swapped_hero_keeps_the_preview_through_the_other_photos(self) -> None:
        page = self.client.get(reverse("pin.details", args=[self.pin.slug])).content.decode()
        overview = self.client.get(reverse("pin.overview", args=[self.pin.slug])).content.decode()

        self.assertIn('hx-swap-oob="true"', overview)
        for content in (page, overview):
            self.assertIn('id="pin-cover-candidates"', content)
            self.assertIn(self.other.display_url, content)
            self.assertIn('data-cover-hero-step="1"', content)

    def test_the_swapped_hero_shows_the_cover_the_page_does(self) -> None:
        overview = self.client.get(reverse("pin.overview", args=[self.pin.slug])).content.decode()

        self.assertIn(f"background-image:url('{self.cover.display_url}')", overview)
