"""Album cover, pin-to-pin move, child listing, same-user dedupe, map hide."""

from __future__ import annotations

from http import HTTPStatus
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album
from urbanlens.dashboard.models.images.issues import PhotoIssueStatus, PhotoMetadataConflict, PhotoUploadFailure
from urbanlens.dashboard.models.images.model import Image, QuotaExemption
from urbanlens.dashboard.services.media.storage import get_storage_used_bytes
from urbanlens.dashboard.services.photos.albums import add_images_to_album, albums_listing, loose_images_for, move_album_to_pin
from urbanlens.dashboard.services.photos.uploads import UploadRejection, upload_photo_for_owner
from urbanlens.dashboard.tests.hypothesis.test_album_view_ux import _PNG_BYTES, _pin_with_album


class AlbumCoverEditTests(TestCase):
    """JSON cover_image_id on the album edit endpoint."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        self.image = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        add_images_to_album(self.album, [self.image], self.pin.profile)
        self.url = reverse("pin.albums.edit", args=[self.pin.slug, self.album.slug])

    def test_json_sets_the_cover_to_a_photo_in_the_album(self) -> None:
        response = self.client.post(
            self.url,
            data=json.dumps({"cover_image_id": self.image.pk}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.album.refresh_from_db()
        self.assertEqual(self.album.cover_image_id, self.image.pk)
        self.assertEqual(response.json()["cover_image_id"], self.image.pk)

    def test_a_photo_not_in_the_album_cannot_be_the_cover(self) -> None:
        outsider = baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        response = self.client.post(
            self.url,
            data=json.dumps({"cover_image_id": outsider.pk}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, HTTPStatus.OK)
        self.album.refresh_from_db()
        self.assertIsNone(self.album.cover_image_id)


class AlbumAlwaysSortableTests(TestCase):
    """The album grid is always sortable; there is no Custom order toggle."""

    def setUp(self) -> None:
        super().setUp()
        self.pin, self.album = _pin_with_album()
        self.client.force_login(self.pin.profile.user)
        baker.make_recipe("dashboard.image", pin=self.pin, profile=self.pin.profile)
        add_images_to_album(self.album, list(Image.objects.filter(pin=self.pin)), self.pin.profile)

    def test_the_detail_grid_is_always_sortable(self) -> None:
        response = self.client.get(reverse("pin.albums", args=[self.pin.slug]), {"album": self.album.slug})

        self.assertContains(response, 'data-album-sortable="1"')
        self.assertContains(response, "data-edit-url")
        self.assertNotContains(response, "Custom order")


class AlbumMoveAndChildrenTests(TestCase):
    """Move an album onto a child pin; list child albums when children=1."""

    def setUp(self) -> None:
        super().setUp()
        self.parent, self.album = _pin_with_album()
        self.client.force_login(self.parent.profile.user)
        self.child = baker.make_recipe("dashboard.detail_pin", profile=self.parent.profile, parent_pin=self.parent)
        self.child.slug = self.child.ensure_slug()
        self.child.save(update_fields=["slug"])
        self.photo = baker.make_recipe("dashboard.image", pin=self.parent, profile=self.parent.profile)
        add_images_to_album(self.album, [self.photo], self.parent.profile)

    def test_move_reparents_the_album_and_its_photos(self) -> None:
        moved = move_album_to_pin(self.album, self.child)

        self.assertEqual(moved.parent_pin_id, self.child.pk)
        self.photo.refresh_from_db()
        self.assertEqual(self.photo.pin_id, self.child.pk)

    def test_move_view_posts_the_target_pin(self) -> None:
        url = reverse("pin.albums.move", args=[self.parent.slug, self.album.slug])
        response = self.client.post(url, data=json.dumps({"pin_slug": self.child.slug}), content_type="application/json")

        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        self.album.refresh_from_db()
        self.assertEqual(self.album.parent_pin_id, self.child.pk)

    def test_children_listing_includes_the_child_album(self) -> None:
        self.album.parent_pin = self.child
        self.album.save(update_fields=["parent_pin", "updated"])

        entries = albums_listing([self.parent, self.child], self.parent.profile)
        self.assertEqual([entry.album.pk for entry in entries], [self.album.pk])

        response = self.client.get(reverse("pin.albums", args=[self.parent.slug]), {"children": "1"})
        self.assertContains(response, self.album.name)
        self.assertContains(response, self.child.effective_name)

    def test_children_listing_includes_child_loose_photos(self) -> None:
        loose = baker.make_recipe("dashboard.image", pin=self.child, profile=self.parent.profile)
        qs = loose_images_for([self.parent, self.child], self.parent.profile)
        self.assertIn(loose.pk, qs.values_list("pk", flat=True))


class SameUserDedupeTests(TestCase):
    """Same bytes on another pin reuse storage and do not double-charge quota."""

    def setUp(self) -> None:
        super().setUp()
        self.pin_a = baker.make_recipe("dashboard.pin")
        self.pin_b = baker.make_recipe("dashboard.pin", profile=self.pin_a.profile)
        self.file = lambda name="shot.png": SimpleUploadedFile(name, _PNG_BYTES, content_type="image/png")

    def test_second_pin_reuses_the_file_and_skips_quota(self) -> None:
        first = upload_photo_for_owner(self.pin_a, self.pin_a.profile, self.file("a.png"))
        self.assertIsInstance(first, Image)
        used_after_first = get_storage_used_bytes(self.pin_a.profile)

        second = upload_photo_for_owner(self.pin_b, self.pin_a.profile, self.file("b.png"))
        self.assertIsInstance(second, Image)
        self.assertEqual(second.quota_exempt_reason, QuotaExemption.DEDUPLICATED)
        self.assertEqual(second.image.name, first.image.name)
        self.assertEqual(get_storage_used_bytes(self.pin_a.profile), used_after_first)
        self.assertEqual(Image.objects.filter(profile=self.pin_a.profile).count(), 2)

    def test_same_pin_is_still_a_conflict(self) -> None:
        first = upload_photo_for_owner(self.pin_a, self.pin_a.profile, self.file())
        self.assertIsInstance(first, Image)
        again = upload_photo_for_owner(self.pin_a, self.pin_a.profile, self.file())
        self.assertIsInstance(again, UploadRejection)
        self.assertEqual(again.status, 409)

    def test_caption_mismatch_queues_a_memories_conflict(self) -> None:
        first = upload_photo_for_owner(self.pin_a, self.pin_a.profile, self.file(), "first caption")
        self.assertIsInstance(first, Image)
        second = upload_photo_for_owner(self.pin_b, self.pin_a.profile, self.file(), "second caption")
        self.assertIsInstance(second, Image)
        conflict = PhotoMetadataConflict.objects.get(profile=self.pin_a.profile)
        self.assertEqual(conflict.status, PhotoIssueStatus.PENDING)
        self.assertIn("caption", conflict.fields)


class MapHiddenTests(TestCase):
    """map_hidden keeps GPS but drops the photo from with_coords()."""

    def test_with_coords_skips_hidden_photos(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        shown = baker.make_recipe(
            "dashboard.image",
            pin=pin,
            profile=pin.profile,
            latitude=40.0,
            longitude=-74.0,
            map_hidden=False,
        )
        baker.make_recipe(
            "dashboard.image",
            pin=pin,
            profile=pin.profile,
            latitude=40.1,
            longitude=-74.1,
            map_hidden=True,
        )
        ids = list(Image.objects.filter(pin=pin).with_coords().values_list("pk", flat=True))
        self.assertEqual(ids, [shown.pk])

    def test_pin_image_post_hides_without_clearing_gps(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        image = baker.make_recipe(
            "dashboard.image",
            pin=pin,
            profile=pin.profile,
            latitude=40.0,
            longitude=-74.0,
        )
        self.client.force_login(pin.profile.user)
        url = reverse("pin.gallery.image", args=[pin.slug, image.pk])
        response = self.client.post(url, data=json.dumps({"map_hidden": True}), content_type="application/json")

        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        image.refresh_from_db()
        self.assertTrue(image.map_hidden)
        self.assertEqual(float(image.latitude), 40.0)
        self.assertEqual(float(image.longitude), -74.0)

    def test_a_drag_clears_map_hidden(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        image = baker.make_recipe(
            "dashboard.image",
            pin=pin,
            profile=pin.profile,
            latitude=40.0,
            longitude=-74.0,
            map_hidden=True,
        )
        self.client.force_login(pin.profile.user)
        url = reverse("pin.gallery.image", args=[pin.slug, image.pk])
        response = self.client.post(
            url,
            data=json.dumps({"latitude": 41.0, "longitude": -75.0}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, HTTPStatus.OK, response.content)
        image.refresh_from_db()
        self.assertFalse(image.map_hidden)


class UploadFailureRecordTests(TestCase):
    """Refused uploads are reviewable on Memories."""

    def test_a_rejected_album_upload_is_recorded(self) -> None:
        pin, album = _pin_with_album()
        self.client.force_login(pin.profile.user)
        url = reverse("pin.albums.upload", args=[pin.slug, album.slug])
        with patch("urbanlens.dashboard.services.photos.uploads.image_upload_error", return_value=("Not an image.", 415)):
            response = self.client.post(url, {"image": SimpleUploadedFile("shot.png", _PNG_BYTES, content_type="image/png")})

        self.assertEqual(response.status_code, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        failure = PhotoUploadFailure.objects.get(profile=pin.profile)
        self.assertEqual(failure.filename, "shot.png")
        self.assertEqual(failure.album_id, album.pk)
        self.assertEqual(failure.status, PhotoIssueStatus.PENDING)
