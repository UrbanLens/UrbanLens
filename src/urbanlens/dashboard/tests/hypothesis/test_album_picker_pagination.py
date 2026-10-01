"""The "add from this place" picker fetches its photos instead of being rendered with them."""

from __future__ import annotations

import json

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.services.photos.albums import add_images_to_album


def _pin_with_photos(count: int):
    pin = baker.make_recipe("dashboard.pin")
    images = [baker.make_recipe("dashboard.image", pin=pin, profile=pin.profile) for _ in range(count)]
    return pin, images


class AlbumPickerDeferralTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin, self.images = _pin_with_photos(5)
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)
        self.client.force_login(self.pin.profile.user)

    def _detail(self):
        return self.client.get(
            reverse("pin.albums.detail", kwargs={"pin_slug": self.pin.slug, "album_slug": self.album.slug})
        )

    def test_the_page_does_not_carry_the_photos_the_picker_will_show(self) -> None:
        response = self._detail()

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("album-add-item", body, "the picker's tiles are still being rendered into the page")
        self.assertIn("data-album-eligible-url", body, "the picker has no endpoint to fetch from")

    def test_the_page_still_knows_whether_there_is_anything_to_add(self) -> None:
        """The count gates the affordance. Dropping it ships a picker nobody can open."""
        response = self._detail()

        self.assertEqual(response.context["available_image_count"], 5)
        self.assertIn("data-album-picker-open", response.content.decode())

    def test_photos_already_in_the_album_are_not_offered(self) -> None:
        add_images_to_album(self.album, self.images[:2], self.pin.profile)

        response = self._detail()

        self.assertEqual(response.context["available_image_count"], 3)

    def test_an_album_holding_everything_offers_no_picker(self) -> None:
        add_images_to_album(self.album, self.images, self.pin.profile)

        response = self._detail()

        self.assertEqual(response.context["available_image_count"], 0)
        self.assertNotIn("data-album-picker-open", response.content.decode())


class AlbumEligibleImagesEndpointTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.pin, self.images = _pin_with_photos(9)
        self.album = Album.objects.create(name="Interior", profile=self.pin.profile, parent_pin=self.pin)
        self.client.force_login(self.pin.profile.user)
        self.url = reverse("pin.albums.eligible", kwargs={"pin_slug": self.pin.slug, "album_slug": self.album.slug})

    def _page(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return json.loads(response.content)

    def test_it_returns_a_bounded_page_and_the_true_total(self) -> None:
        data = self._page(limit=4)

        self.assertEqual(len(data["items"]), 4)
        self.assertEqual(data["total"], 9, "the total must count everything, or the picker stops asking too early")

    def test_the_rest_is_reachable(self) -> None:
        """The assertion that separates paginating from capping."""
        seen: set[int] = set()
        for offset in (0, 4, 8):
            seen |= {item["id"] for item in self._page(limit=4, offset=offset)["items"]}

        self.assertEqual(len(seen), 9)

    def test_photos_already_in_the_album_are_excluded(self) -> None:
        add_images_to_album(self.album, self.images[:3], self.pin.profile)

        data = self._page(limit=50)

        offered = {item["id"] for item in data["items"]}
        self.assertEqual(data["total"], 6)
        self.assertEqual(offered & {image.pk for image in self.images[:3]}, set())

    def test_a_tile_carries_the_same_fields_the_album_grid_gets(self) -> None:
        """A page of ids the client cannot draw is not a page.

        Compared against ``AlbumItemsView``'s own payload rather than against a hand-written list: both go
        through ``_photo_tile``, and holding them to each other is what catches one of them drifting."""
        add_images_to_album(self.album, self.images[:1], self.pin.profile)
        items_url = reverse("pin.albums.items", kwargs={"pin_slug": self.pin.slug, "album_slug": self.album.slug})

        offered = self._page(limit=1)["items"][0]
        in_album = json.loads(self.client.get(items_url, {"limit": 1}).content)["items"][0]

        self.assertIn("id", offered)
        self.assertIn("thumb_url", offered)
        self.assertEqual(set(offered) - {"item_id"}, set(in_album) - {"item_id"})

    def test_another_profile_cannot_page_this_album(self) -> None:
        other = baker.make_recipe("dashboard.pin")
        self.client.force_login(other.profile.user)

        response = self.client.get(self.url)

        self.assertIn(response.status_code, (403, 404))

    def test_an_absurd_limit_is_clamped(self) -> None:
        """The endpoint must not be a way to ask for everything after all."""
        data = self._page(limit=100000)

        self.assertLessEqual(len(data["items"]), 100)
