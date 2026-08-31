"""Tests for Vault (Profile-owned) albums: model scoping and the view layer.

Vault albums reuse the same Album/AlbumItem models and the same view classes
as pin/wiki albums (controllers.albums), just resolved with ``vault=True``
instead of a pin/location slug - see services/photos/albums.py and
controllers/albums.py's Pin | Wiki | Profile widening.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client
from django.urls import NoReverseMatch, reverse
from model_bakery import baker
import pytest

from urbanlens.core.tests.images import JPEG_BYTES
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.services.photos.uploads import UploadRejection, upload_photo_for_owner

_ALBUMS_URL = reverse("vault.photos.albums")


def _album_url(name: str, album_slug: str) -> str:
    return reverse(name, args=[album_slug])


class VaultAlbumModelTests(TestCase):
    """Album.parent_profile scoping and the exactly-one-owner constraint."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_two_vault_albums_may_share_a_name_with_a_pin_album(self) -> None:
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        pin_album = Album.objects.create(name="Interior", profile=self.profile, parent_pin=pin)
        vault_album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        self.assertEqual(pin_album.slug, vault_album.slug)

    def test_one_vault_cannot_reuse_a_slug(self) -> None:
        first = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        second = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        self.assertNotEqual(first.slug, second.slug)

    def test_two_profiles_scope_independently(self) -> None:
        other = baker.make(User).profile
        Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        Album.objects.create(name="Interior", profile=other, parent_profile=other)
        self.assertEqual(Album.objects.for_profile(self.profile).count(), 1)
        self.assertEqual(Album.objects.for_profile(other).count(), 1)

    def test_exactly_one_owner_is_enforced_at_the_db_level(self) -> None:
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        with pytest.raises(IntegrityError) as both, transaction.atomic():
            Album.objects.create(name="Both", profile=self.profile, parent_pin=pin, parent_profile=self.profile)
        self.assertIn("ck_album_exactly_one_owner", str(both.value))
        with pytest.raises(IntegrityError) as neither, transaction.atomic():
            Album.objects.create(name="Neither", profile=self.profile)
        self.assertIn("ck_album_exactly_one_owner", str(neither.value))


class VaultAlbumViewTests(TestCase):
    """The Vault album routes: create, edit, delete, add/remove/reorder photos, upload."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.other_user: User = baker.make(User)
        self.other_profile = self.other_user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def test_create_lists_and_shows_a_vault_album(self) -> None:
        response = self.client.post(_ALBUMS_URL, {"name": "Interior", "description": ""})
        self.assertEqual(response.status_code, 200)
        album = Album.objects.get(parent_profile=self.profile, name="Interior")
        self.assertContains(self.client.get(_ALBUMS_URL), "Interior")

        detail = self.client.get(_album_url("vault.photos.albums.detail", album.slug))
        self.assertEqual(detail.status_code, 200)

    def test_add_and_remove_photos(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None)

        add = self.client.post(
            _album_url("vault.photos.albums.add", album.slug),
            data={"image_ids": [image.pk]},
            content_type="application/json",
        )
        self.assertEqual(add.status_code, 200)
        self.assertEqual(add.json()["added"], 1)
        self.assertTrue(AlbumItem.objects.filter(album=album, image=image).exists())

        remove = self.client.post(
            _album_url("vault.photos.albums.remove", album.slug),
            data={"image_ids": [image.pk]},
            content_type="application/json",
        )
        self.assertEqual(remove.status_code, 200)
        self.assertEqual(remove.json()["removed"], 1)
        self.assertFalse(AlbumItem.objects.filter(album=album, image=image).exists())

    def test_cannot_add_another_profiles_photo(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        others_image = baker.make(Image, profile=self.other_profile, pin=None, wiki=None)

        response = self.client.post(
            _album_url("vault.photos.albums.add", album.slug),
            data={"image_ids": [others_image.pk]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 0)
        self.assertFalse(AlbumItem.objects.filter(album=album, image=others_image).exists())

    def test_can_add_own_pin_filed_photo_to_vault_album(self) -> None:
        """A vault album may reference any of the profile's own uploads, filed or not.

        This is deliberate, not a gap: Vault Photos' own gallery already shows
        every photo the profile has uploaded regardless of pin/wiki filing, so
        a vault album (a curated subset of that same library) can hold a
        photo that's also filed to one of the profile's pins. Adding it here
        doesn't move or duplicate it - AlbumItem already supports an image
        belonging to more than one album. See owner_kwargs_to_image_scope's
        docstring in services/photos/albums.py.
        """
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        pin_photo = baker.make(Image, profile=self.profile, pin=pin, wiki=None)

        response = self.client.post(
            _album_url("vault.photos.albums.add", album.slug),
            data={"image_ids": [pin_photo.pk]},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 1)
        self.assertTrue(AlbumItem.objects.filter(album=album, image=pin_photo).exists())
        pin_photo.refresh_from_db()
        self.assertEqual(pin_photo.pin_id, pin.pk)

    def test_external_media_add_is_refused_for_a_vault_album(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        response = self.client.post(
            _album_url("vault.photos.albums.add", album.slug),
            data={"media": {"source": "wikimedia", "url": "https://example.com/x.jpg"}},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["added"], 0)
        self.assertEqual(body.get("error"), "Vault albums can only hold your own uploaded photos, not external media.")
        self.assertEqual(AlbumItem.objects.filter(album=album).count(), 0)

    def test_reorder_persists_custom_order(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        images = [baker.make(Image, profile=self.profile, pin=None, wiki=None) for _ in range(3)]
        for image in images:
            AlbumItem.objects.create(album=album, image=image)
        items = list(AlbumItem.objects.filter(album=album).order_by("pk"))
        new_order = [items[2].pk, items[0].pk, items[1].pk]

        response = self.client.post(
            _album_url("vault.photos.albums.reorder", album.slug),
            data={"items": new_order},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reordered"], 3)
        reordered = list(AlbumItem.objects.filter(album=album).order_by("order").values_list("pk", flat=True))
        self.assertEqual(reordered, new_order)

    def test_edit_renames_the_album(self) -> None:
        album = Album.objects.create(name="Old name", profile=self.profile, parent_profile=self.profile)
        response = self.client.post(_album_url("vault.photos.albums.edit", album.slug), {"name": "New name"})
        self.assertEqual(response.status_code, 200)
        album.refresh_from_db()
        self.assertEqual(album.name, "New name")

    def test_delete_removes_the_album_but_keeps_the_photos(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None)
        AlbumItem.objects.create(album=album, image=image)

        response = self.client.post(_album_url("vault.photos.albums.delete", album.slug))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Album.objects.filter(pk=album.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())

    def test_another_profile_cannot_reach_this_profiles_album(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        self.client.force_login(self.other_user)
        response = self.client.get(_album_url("vault.photos.albums.detail", album.slug))
        self.assertEqual(response.status_code, 404)

    def test_upload_into_album_files_the_photo_in_it(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        upload = self.client.post(
            _album_url("vault.photos.albums.upload", album.slug),
            {"image": SimpleUploadedFile("photo.jpg", JPEG_BYTES, content_type="image/jpeg")},
        )
        self.assertEqual(upload.status_code, 201)
        image = Image.objects.get(profile=self.profile)
        self.assertIsNone(image.pin_id)
        self.assertIsNone(image.wiki_id)
        self.assertTrue(AlbumItem.objects.filter(album=album, image=image).exists())

    def test_items_endpoint_pages_the_albums_photos(self) -> None:
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        images = [baker.make(Image, profile=self.profile, pin=None, wiki=None) for _ in range(3)]
        for image in images:
            AlbumItem.objects.create(album=album, image=image)

        response = self.client.get(_album_url("vault.photos.albums.items", album.slug), {"offset": 0, "limit": 2})
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["items"]), 2)

    def test_no_move_route_exists_for_a_vault_album(self) -> None:
        with pytest.raises(NoReverseMatch):
            reverse("vault.photos.albums.move", args=["some-slug"])

    def test_the_pin_move_url_404s_for_a_vault_albums_slug(self) -> None:
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        album = Album.objects.create(name="Interior", profile=self.profile, parent_profile=self.profile)
        response = self.client.post(reverse("pin.albums.move", args=[pin.slug, album.slug]), {"pin_slug": pin.slug})
        self.assertEqual(response.status_code, 404)

    def test_loose_photos_section_is_suppressed_for_the_vault_albums_list(self) -> None:
        """Vault Photos has its own full gallery, so the album-list page's
        "loose photos" section (meant for a pin/wiki's unfiled photos) would
        just repeat it - _albums_panel.html hides it for context_type=='vault'.
        """
        baker.make(Image, profile=self.profile, pin=None, wiki=None)
        response = self.client.get(_ALBUMS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "albums-loose-grid")


class VaultPinAlbumsViewTests(TestCase):
    """The read-only cross-pin album listing behind the Vault's toggle."""

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def test_lists_albums_across_the_profiles_own_pins(self) -> None:
        pin_a = baker.make_recipe("dashboard.pin", profile=self.profile)
        pin_b = baker.make_recipe("dashboard.pin", profile=self.profile)
        Album.objects.create(name="From pin A", profile=self.profile, parent_pin=pin_a)
        Album.objects.create(name="From pin B", profile=self.profile, parent_pin=pin_b)

        response = self.client.get(reverse("vault.photos.pin_albums"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "From pin A")
        self.assertContains(response, "From pin B")

    def test_excludes_another_profiles_pin_albums(self) -> None:
        other_pin = baker.make_recipe("dashboard.pin")
        Album.objects.create(name="Not mine", profile=other_pin.profile, parent_pin=other_pin)

        response = self.client.get(reverse("vault.photos.pin_albums"))
        self.assertNotContains(response, "Not mine")

    def test_excludes_vault_albums_from_the_pin_listing(self) -> None:
        Album.objects.create(name="My vault album", profile=self.profile, parent_profile=self.profile)

        response = self.client.get(reverse("vault.photos.pin_albums"))
        self.assertNotContains(response, "My vault album")


class VaultUploadDedupeTests(TestCase):
    """The widened, unfiled-only duplicate-upload scope for a Vault (Profile) owner.

    Distinct from owner_kwargs_to_image_scope (which deliberately scopes a
    vault's *eligible* photos to everything the profile has uploaded): this is
    about whether a fresh *upload* is a duplicate, which stays narrower - see
    uploads.py's _duplicate_scope.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.file = lambda name="shot.jpg": SimpleUploadedFile(name, JPEG_BYTES, content_type="image/jpeg")

    def test_same_bytes_uploaded_to_the_vault_twice_is_a_conflict(self) -> None:
        first = upload_photo_for_owner(self.profile, self.profile, self.file("a.jpg"))
        self.assertIsInstance(first, Image)

        again = upload_photo_for_owner(self.profile, self.profile, self.file("b.jpg"))
        self.assertIsInstance(again, UploadRejection)
        self.assertEqual(again.status, 409)
        self.assertIn("vault", again.message)

    def test_bytes_already_filed_to_a_pin_get_a_deduped_copy_in_the_vault(self) -> None:
        pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        filed = upload_photo_for_owner(pin, self.profile, self.file("a.jpg"))
        self.assertIsInstance(filed, Image)

        vault_copy = upload_photo_for_owner(self.profile, self.profile, self.file("b.jpg"))
        self.assertIsInstance(vault_copy, Image)
        self.assertNotEqual(vault_copy.pk, filed.pk)
        self.assertIsNone(vault_copy.pin_id)
        self.assertIsNone(vault_copy.wiki_id)
        self.assertEqual(vault_copy.quota_exempt_reason, QuotaExemption.DEDUPLICATED)
        self.assertEqual(vault_copy.image.name, filed.image.name)
