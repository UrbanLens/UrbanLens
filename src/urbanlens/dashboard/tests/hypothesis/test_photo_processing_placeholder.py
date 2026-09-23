"""A photo whose re-encode has not landed is listed as processing, never by its stored path (P142).

The stored file of a pending upload is replaced, and deleted, by the re-encode; a listing that names
it hands the browser a URL that 404s moments later.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.albums import _photo_tile
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.media.images import image_to_gallery_json

_RAW = "pin_images/raw0p142/upload-p142.jpg"
_THUMB = "pin_images/thumbp142/thumb-p142.webp"
_ENCODED = "pin_images/encp142/encoded-p142.webp"
_STATUS_URL = reverse("vault.photos.processing")


def _pending(profile, **fields) -> Image:
    return baker.make(
        Image, profile=profile, pin=None, wiki=None, image=_RAW, thumbnail=None, pending_scan=True, **fields
    )


def _ready(profile, **fields) -> Image:
    return baker.make(Image, profile=profile, pin=None, wiki=None, image=_ENCODED, thumbnail=_THUMB, **fields)


class ProcessingStateTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def test_a_pending_upload_is_processing(self) -> None:
        image = _pending(self.profile)
        self.assertTrue(image.is_processing)
        self.assertFalse(image.processing_failed)

    def test_a_reencoded_photo_is_not(self) -> None:
        image = _ready(self.profile)
        self.assertFalse(image.is_processing)
        self.assertFalse(image.processing_failed)

    def test_a_pending_upload_whose_processing_failed_is_failed_not_processing(self) -> None:
        image = _pending(self.profile, upload_failed_at=timezone.now())
        self.assertFalse(image.is_processing)
        self.assertTrue(image.processing_failed)


class GalleryJsonTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.request = RequestFactory().get("/")

    def test_a_processing_photo_names_no_file(self) -> None:
        payload = image_to_gallery_json(_pending(self.profile), self.request, self.profile)
        self.assertIs(payload["processing"], True)
        self.assertIs(payload["processing_failed"], False)
        self.assertEqual((payload["url"], payload["thumb_url"], payload["marker_thumb_url"]), ("", "", ""))
        self.assertNotIn(_RAW, json.dumps(payload))

    def test_a_failed_photo_names_no_file(self) -> None:
        payload = image_to_gallery_json(
            _pending(self.profile, upload_failed_at=timezone.now()), self.request, self.profile
        )
        self.assertIs(payload["processing"], False)
        self.assertIs(payload["processing_failed"], True)
        self.assertNotIn(_RAW, json.dumps(payload))

    def test_a_reencoded_photo_names_its_files(self) -> None:
        payload = image_to_gallery_json(_ready(self.profile), self.request, self.profile)
        self.assertIs(payload["processing"], False)
        self.assertIn(_ENCODED, payload["url"])
        self.assertIn(_THUMB, payload["thumb_url"])

    def test_the_album_tile_payload_does_not_restore_the_stored_path(self) -> None:
        payload = _photo_tile(_pending(self.profile), self.request, self.profile)
        self.assertEqual(payload["thumb_url"], "")
        self.assertNotIn(_RAW, json.dumps(payload))


class VaultListingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_items_endpoint_lists_a_processing_photo_without_its_path(self) -> None:
        image = _pending(self.profile)
        response = self.client.get(reverse("vault.photos.items"))
        self.assertNotIn(_RAW, response.content.decode())
        [item] = response.json()["items"]
        self.assertEqual(item["id"], image.pk)
        self.assertIs(item["processing"], True)

    def test_the_server_rendered_grid_shows_a_placeholder(self) -> None:
        image = _pending(self.profile)
        response = self.client.get(reverse("vault.photos"))
        body = response.content.decode()
        self.assertNotIn(_RAW, body)
        self.assertContains(response, f'id="photo-tile-{image.pk}"')
        self.assertContains(response, 'data-processing="pending"')
        self.assertContains(response, 'aria-label="Processing…"')

    def test_the_upload_response_is_processing(self) -> None:
        with mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            response = self.client.post(
                reverse("vault.photos.upload"),
                {"image": SimpleUploadedFile("p142.jpg", _tiny_jpeg(), content_type="image/jpeg")},
            )
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertIs(body["processing"], True)
        self.assertEqual(body["thumb_url"], "")


class PinGalleryTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.pin = baker.make(Pin, profile=self.profile, slug="p142-pin")
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_pin_gallery_shows_a_placeholder(self) -> None:
        image = baker.make(Image, profile=self.profile, pin=self.pin, image=_RAW, thumbnail=None, pending_scan=True)
        response = self.client.get(reverse("pin.gallery", args=[self.pin.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(_RAW, response.content.decode())
        self.assertContains(response, f'id="gallery-item-{image.pk}"')
        self.assertContains(response, 'aria-label="Processing…"')


class ProcessingStatusViewTests(TestCase):
    """GET /vault/photos/processing/?ids= - what a placeholder tile polls until its photo is ready."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def _get(self, *ids: object) -> dict:
        response = self.client.get(_STATUS_URL, {"ids": ",".join(str(i) for i in ids)})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_a_still_pending_photo_is_listed_as_processing(self) -> None:
        image = _pending(self.profile)
        body = self._get(image.pk)
        self.assertEqual(body["processing"], [image.pk])
        self.assertEqual(body["items"], [])

    def test_a_reencoded_photo_comes_back_with_its_new_file(self) -> None:
        image = _pending(self.profile)
        Image.objects.filter(pk=image.pk).update(image=_ENCODED, thumbnail=_THUMB, pending_scan=False)
        body = self._get(image.pk)
        self.assertEqual(body["processing"], [])
        [item] = body["items"]
        self.assertEqual(item["id"], image.pk)
        self.assertIs(item["processing"], False)
        self.assertIn(_THUMB, item["thumb_url"])

    def test_a_failed_photo_settles_as_failed(self) -> None:
        image = _pending(self.profile, upload_failed_at=timezone.now())
        body = self._get(image.pk)
        self.assertEqual(body["processing"], [])
        [item] = body["items"]
        self.assertIs(item["processing_failed"], True)

    def test_a_deleted_or_foreign_photo_is_in_neither_list(self) -> None:
        foreign = _pending(baker.make(User).profile)
        gone = _pending(self.profile)
        gone_pk = gone.pk
        gone.delete()
        body = self._get(foreign.pk, gone_pk)
        self.assertEqual(body, {"items": [], "processing": []})

    def test_junk_ids_are_ignored(self) -> None:
        image = _pending(self.profile)
        body = self._get("x", "", -3, image.pk)
        self.assertEqual(body["processing"], [image.pk])

    def test_the_id_list_is_capped(self) -> None:
        with mock.patch("urbanlens.dashboard.controllers.vault_photos._PROCESSING_STATUS_MAX_IDS", 2):
            images = [_pending(self.profile) for _ in range(3)]
            body = self._get(*(image.pk for image in images))
        self.assertEqual(len(body["processing"]), 2)

    def test_documents_are_listed_like_photos(self) -> None:
        document = _pending(self.profile, media_type=MediaKind.DOCUMENT)
        self.assertEqual(self._get(document.pk)["processing"], [document.pk])

    def test_login_is_required(self) -> None:
        self.client.logout()
        response = self.client.get(_STATUS_URL, {"ids": "1"})
        self.assertEqual(response.status_code, 302)


def _tiny_jpeg() -> bytes:
    from io import BytesIO

    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new("RGB", (4, 4), (120, 40, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()
