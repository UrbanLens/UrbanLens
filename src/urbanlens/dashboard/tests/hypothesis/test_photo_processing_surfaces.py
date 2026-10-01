"""Every surface that lists a user's upload names no file while it is still being processed (P142).

The raw file of a pending upload is deleted once its re-encode lands, so any URL for it 404s moments later.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
import json

from django.contrib.auth.models import User
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.albums import _photo_map_payload
from urbanlens.dashboard.external_api.serializers import SafetyPhotoSerializer, build_photo_payload
from urbanlens.dashboard.external_api.views_wiki import _gallery_row
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.map_pins import MapPinPayloadService
from urbanlens.dashboard.services.media.images import image_to_gallery_json
from urbanlens.dashboard.services.memories.aggregator import _photos_for_range

_RAW = "pin_images/raw0p142s/upload-p142s.jpg"
_RAW_DOC = "pin_images/raw0p142s/vault-doc-p142s.txt"
_ENCODED = "pin_images/encp142s/encoded-p142s.webp"
_THUMB = "pin_images/thumbp142s/thumb-p142s.webp"
_MARKER = "pin_images/markerp142s/marker-p142s.webp"
_TAKEN = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


def _pending(profile, **fields) -> Image:
    fields = {"image": _RAW, "thumbnail": None, "marker_thumbnail": None, **fields}
    return baker.make(Image, profile=profile, pending_scan=True, **fields)


def _ready(profile, **fields) -> Image:
    return baker.make(Image, profile=profile, image=_ENCODED, thumbnail=_THUMB, marker_thumbnail=_MARKER, **fields)


class _Owner(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)


class ImageUrlPropertyTests(_Owner):
    def test_a_pending_upload_has_no_urls(self) -> None:
        image = _pending(self.profile, thumbnail=_THUMB, marker_thumbnail=_MARKER)
        self.assertEqual((image.display_url, image.thumb_url, image.marker_thumb_url), ("", "", ""))
        self.assertIsNone(image.file_url)

    def test_a_failed_upload_has_no_urls(self) -> None:
        image = _pending(self.profile, upload_failed_at=timezone.now())
        self.assertEqual((image.display_url, image.thumb_url, image.marker_thumb_url), ("", "", ""))
        self.assertIsNone(image.file_url)

    def test_a_processed_upload_has_its_urls(self) -> None:
        image = _ready(self.profile)
        self.assertIn(_ENCODED, image.display_url)
        self.assertIn(_ENCODED, image.file_url or "")
        self.assertIn(_THUMB, image.thumb_url)
        self.assertIn(_MARKER, image.marker_thumb_url)

    def test_servable_drops_pending_rows(self) -> None:
        pending = _pending(self.profile)
        ready = _ready(self.profile)
        self.assertEqual(list(Image.objects.filter(pk__in=[pending.pk, ready.pk]).servable()), [ready])


class VaultDocumentsTests(_Owner):
    def test_gallery_json_names_no_file_for_a_pending_document(self) -> None:
        document = _pending(self.profile, image=_RAW_DOC, media_type=MediaKind.DOCUMENT)
        payload = image_to_gallery_json(document, RequestFactory().get("/"), self.profile)
        self.assertIs(payload["processing"], True)
        self.assertEqual(payload["url"], "")
        self.assertNotIn(_RAW_DOC, json.dumps(payload))

    def test_the_documents_page_shows_a_placeholder(self) -> None:
        document = _pending(self.profile, image=_RAW_DOC, media_type=MediaKind.DOCUMENT, caption="p142s.txt")
        response = self.client.get(reverse("vault.documents"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(_RAW_DOC, response.content.decode())
        self.assertContains(response, f'id="document-tile-{document.pk}"')
        self.assertContains(response, 'data-processing="pending"')
        self.assertContains(response, f'data-processing-url="{reverse("vault.photos.processing")}"')

    def test_the_items_endpoint_names_no_file(self) -> None:
        _pending(self.profile, image=_RAW_DOC, media_type=MediaKind.DOCUMENT)
        response = self.client.get(reverse("vault.documents.items"))
        self.assertNotIn(_RAW_DOC, response.content.decode())
        [item] = response.json()["items"]
        self.assertIs(item["processing"], True)


class PinMediaPhotosPreviewTests(_Owner):
    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.profile)

    def _get(self):
        return self.client.get(reverse("pin.media", kwargs={"pin_slug": self.pin.slug, "source": "photos"}))

    def test_a_pending_photo_is_a_placeholder_tile(self) -> None:
        image = _pending(self.profile, pin=self.pin)
        response = self._get()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn(_RAW, body)
        self.assertIn(f'data-id="{image.pk}"', body)
        self.assertIn('data-processing="pending"', body)
        self.assertIn(f'data-processing-url="{reverse("vault.photos.processing")}"', body)
        self.assertIn('aria-label="Processing…"', body)

    def test_a_processed_photo_is_shown_by_its_file(self) -> None:
        _ready(self.profile, pin=self.pin)
        body = self._get().content.decode()
        self.assertIn(_ENCODED, body)
        self.assertNotIn("data-processing=", body)


class HomeAndVaultHomeTests(_Owner):
    def test_the_home_recent_photos_widget_shows_a_placeholder(self) -> None:
        image = _pending(self.profile, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("home.view"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn(_RAW, body)
        self.assertIn(f'data-id="{image.pk}"', body)
        self.assertIn('data-processing="pending"', body)
        self.assertIn(f'data-processing-url="{reverse("vault.photos.processing")}"', body)

    def test_the_vault_home_recent_strip_shows_a_placeholder(self) -> None:
        image = _pending(self.profile, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.home"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn(_RAW, body)
        self.assertIn(f'data-id="{image.pk}"', body)
        self.assertIn('data-processing="pending"', body)

    def test_the_vault_home_video_list_does_not_link_a_pending_video(self) -> None:
        raw_video = "pin_images/raw0p142s/clip-p142s.mov"
        _pending(self.profile, image=raw_video, media_type=MediaKind.VIDEO, file_size=10)
        response = self.client.get(reverse("vault.home"))
        self.assertNotIn(raw_video, response.content.decode())


class ExternalApiTests(_Owner):
    def test_photo_payload_is_processing_with_no_url(self) -> None:
        payload = build_photo_payload(_pending(self.profile), self.profile)
        self.assertIs(payload["processing"], True)
        self.assertIs(payload["processing_failed"], False)
        self.assertIsNone(payload["url"])

    def test_photo_payload_of_a_failed_upload(self) -> None:
        payload = build_photo_payload(_pending(self.profile, upload_failed_at=timezone.now()), self.profile)
        self.assertIs(payload["processing"], False)
        self.assertIs(payload["processing_failed"], True)
        self.assertIsNone(payload["url"])

    def test_photo_payload_of_a_processed_photo(self) -> None:
        payload = build_photo_payload(_ready(self.profile), self.profile)
        self.assertIs(payload["processing"], False)
        self.assertIn(_ENCODED, payload["url"])

    def test_the_photos_endpoint_lists_a_pending_photo_without_its_path(self) -> None:
        image = _pending(self.profile)
        api_key, raw_key = generate_api_key(self.user, "p142s")
        api_key.scopes = [ApiKeyScope.PHOTOS_READ.value]
        api_key.save(update_fields=["scopes"])
        response = Client().get(reverse("external_api:photos"), HTTP_AUTHORIZATION=f"Bearer {raw_key}")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn(_RAW, response.content.decode())
        [row] = [row for row in response.json()["results"] if row["uuid"] == str(image.uuid)]
        self.assertIs(row["processing"], True)
        self.assertIsNone(row["url"])

    def test_safety_photo_is_processing_with_no_url(self) -> None:
        data = SafetyPhotoSerializer(_pending(self.profile)).data
        self.assertIs(data["processing"], True)
        self.assertIs(data["processing_failed"], False)
        self.assertIsNone(data["url"])

    def test_wiki_gallery_row_is_processing_with_no_url(self) -> None:
        row = _gallery_row(_pending(self.profile))
        self.assertIs(row["processing"], True)
        self.assertIsNone(row["url"])
        ready = _gallery_row(_ready(self.profile))
        self.assertIs(ready["processing"], False)
        self.assertIn(_ENCODED, ready["url"])

    def test_the_schema_documents_the_processing_fields(self) -> None:
        schema = self.client.get("/dashboard/api/external/v1/schema/", HTTP_ACCEPT="application/json").json()
        components = schema["components"]["schemas"]
        for name in ("Photo", "SafetyPhoto", "GalleryImage"):
            properties = components[name]["properties"]
            self.assertIn("processing", properties, name)
            self.assertIn("processing_failed", properties, name)
            self.assertTrue(properties["url"].get("nullable"), name)


class MapPhotoLayerTests(_Owner):
    def test_the_pin_map_layer_leaves_out_a_pending_photo(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        pending = _pending(self.profile, pin=pin, latitude=Decimal("40.1"), longitude=Decimal("-74.1"))
        ready = _ready(self.profile, pin=pin, latitude=Decimal("40.2"), longitude=Decimal("-74.2"))
        response = self.client.get(reverse("pin.gallery.json", args=[pin.slug]))
        self.assertEqual(response.status_code, 200)
        ids = [image["id"] for image in response.json()["images"]]
        self.assertIn(ready.pk, ids)
        self.assertNotIn(pending.pk, ids)

    def test_the_album_map_layer_leaves_out_a_pending_photo(self) -> None:
        pending = _pending(self.profile, latitude=Decimal("40.1"), longitude=Decimal("-74.1"))
        ready = _ready(self.profile, latitude=Decimal("40.2"), longitude=Decimal("-74.2"))
        ids = [entry["id"] for entry in _photo_map_payload([pending, ready], self.profile)]
        self.assertEqual(ids, [ready.pk])

    def test_memories_leave_out_a_pending_photo(self) -> None:
        pending = _pending(self.profile, latitude=Decimal("40.1"), longitude=Decimal("-74.1"), taken_at=_TAKEN)
        ready = _ready(self.profile, latitude=Decimal("40.2"), longitude=Decimal("-74.2"), taken_at=_TAKEN)
        day = date(2026, 5, 4)
        ids = [event.extra["image_id"] for event in _photos_for_range(self.profile, day, day, None)]
        self.assertIn(ready.pk, ids)
        self.assertNotIn(pending.pk, ids)


class MapPinCoverTests(_Owner):
    def setUp(self) -> None:
        super().setUp()
        location = baker.make(Location, official_name="P142 place", latitude="40.0", longitude="-74.0")
        self.pin = baker.make(Pin, profile=self.profile, location=location, name="P142 pin")

    def _urls(self) -> tuple[str | None, str | None]:
        service = MapPinPayloadService(self.profile)
        query = Pin.objects.filter(pk=self.pin.pk)
        serialized = service.serialize(service.prepare_queryset(query).get())["cover_photo_url"]
        [row] = service.all(query)
        return serialized, row["cover_photo_url"]

    def test_a_pending_photo_is_not_the_fallback_cover(self) -> None:
        _pending(self.profile, pin=self.pin, media_type=MediaKind.PHOTO)
        self.assertEqual(self._urls(), (None, None))

    def test_a_pending_explicit_cover_names_no_file(self) -> None:
        cover = _pending(self.profile, pin=self.pin, media_type=MediaKind.PHOTO)
        Pin.objects.filter(pk=self.pin.pk).update(cover_photo=cover)
        self.pin.refresh_from_db()
        for url in self._urls():
            self.assertFalse(url)
