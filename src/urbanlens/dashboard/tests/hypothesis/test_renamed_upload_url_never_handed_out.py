"""The uploader is never handed the URL of a file that the upload's re-encode renames away (P58).

The re-encode stores the upload under a new name and deletes the old file, after which the old path has no
owning row and 404s, correctly. These run a real upload and a real ``process_image_upload`` and check that
nothing the uploader was shown in between named the old file, and that what they get once it settles is served.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import tempfile
from unittest import mock
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import FileResponse
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.tasks import process_image_upload

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_LIGHTBOX_SELECTOR = ".document-tile[data-id]:not([data-processing])"
_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]


def _jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (900, 700), color=(120, 60, 30)).save(buf, format="JPEG")
    return buf.getvalue()


def _fake_soffice(args: list[str], **_kwargs) -> None:
    """Stand in for LibreOffice: write ``source.pdf`` into the ``--outdir`` it was given."""
    outdir = Path(args[args.index("--outdir") + 1])
    (outdir / "source.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")


def _media_path(url: str) -> str:
    return urlsplit(url).path


def _stored_name(image: Image) -> str:
    name = image.image.name
    assert name
    return name


class _UploadLifecycle(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix="ul_p58_")
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        overrides = override_settings(MEDIA_ROOT=self.media_root, MEDIA_X_ACCEL=False)
        overrides.enable()
        self.addCleanup(overrides.disable)
        site_settings = SiteSettings.get_current()
        site_settings.image_convert_webp = True
        site_settings.save(update_fields=["image_convert_webp"])
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.shown: dict[str, str] = {}

    def show(self, label: str, url: str, params: dict | None = None) -> None:
        response = self.client.get(url, params or {})
        self.assertEqual(response.status_code, 200, label)
        self.shown[label] = response.content.decode()

    def upload(self, url: str, field: str, file: SimpleUploadedFile) -> Image:
        with mock.patch(_ENQUEUE):
            response = self.client.post(url, {field: file})
        self.assertEqual(response.status_code, 201, response.content)
        self.shown["upload response"] = response.content.decode()
        image = Image.objects.get(pk=response.json()["id"])
        self.assertTrue(image.pending_scan)
        return image

    def status(self, image: Image) -> dict:
        response = self.client.get(reverse("vault.photos.processing"), {"ids": str(image.pk)})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def assert_nothing_shown_names(self, name: str) -> None:
        for label, body in self.shown.items():
            self.assertNotIn(name, body, f"{label} handed out the file the re-encode deletes")

    def assert_served(self, url: str) -> None:
        response = self.client.get(_media_path(url))
        self.assertEqual(response.status_code, 200, url)
        if isinstance(response, FileResponse) and response.file_to_stream is not None:
            response.file_to_stream.close()

    def assert_renamed_away(self, image: Image, raw_name: str) -> None:
        image.refresh_from_db()
        self.assertFalse(image.pending_scan)
        self.assertNotEqual(image.image.name, raw_name, "nothing was renamed, so this proves nothing")
        self.assertFalse(image.image.storage.exists(raw_name))
        self.assertEqual(self.client.get(f"/media/{raw_name}").status_code, 404)


class PhotoUploadTests(_UploadLifecycle):
    def test_no_surface_names_the_upload_and_the_settled_tile_is_served(self) -> None:
        image = self.upload(
            reverse("vault.photos.upload"), "image", SimpleUploadedFile("p58.jpg", _jpeg_bytes(), "image/jpeg")
        )
        raw_name = _stored_name(image)
        self.show("Vault Photos page", reverse("vault.photos"))
        self.show("Vault Photos items", reverse("vault.photos.items"))
        self.show("processing status", reverse("vault.photos.processing"), {"ids": str(image.pk)})
        self.assertEqual(self.status(image)["processing"], [image.pk])

        process_image_upload(image.pk)

        self.assert_renamed_away(image, raw_name)
        self.assert_nothing_shown_names(raw_name)
        [item] = self.status(image)["items"]
        self.assertIn(image.image.name, item["url"])
        self.assert_served(item["url"])
        self.assert_served(item["thumb_url"])

    def test_the_stable_link_holds_a_placeholder_then_follows_the_rename(self) -> None:
        image = self.upload(
            reverse("vault.photos.upload"), "image", SimpleUploadedFile("p58.jpg", _jpeg_bytes(), "image/jpeg")
        )
        link = reverse("media.image", args=[image.uuid])
        pending = self.client.get(link)
        self.assertEqual(pending["Content-Type"], "image/svg+xml")
        self.assertEqual(pending["Cache-Control"], "no-store")

        process_image_upload(image.pk)

        image.refresh_from_db()
        settled = self.client.get(link)
        self.assertEqual(settled.status_code, 302)
        self.assertEqual(settled["Location"], image.image.url)


@mock.patch("urbanlens.dashboard.services.media.documents.extract_pdf_text", return_value=None)
@mock.patch("urbanlens.dashboard.services.media.documents.subprocess.run", side_effect=_fake_soffice)
@mock.patch("urbanlens.dashboard.services.media.documents.soffice_path", return_value="/usr/bin/soffice")
class DocumentUploadTests(_UploadLifecycle):
    """A converting document is renamed ``.txt`` -> ``.pdf``, and the lightbox iframe loads a tile's ``data-url``."""

    def upload_document(self) -> Image:
        document = self.upload(
            reverse("vault.documents.upload"),
            "document",
            SimpleUploadedFile("p58-notes.txt", b"minutes of the meeting", "text/plain"),
        )
        self.assertEqual(document.media_type, MediaKind.DOCUMENT)
        return document

    def test_no_surface_names_the_upload(self, *_mocks) -> None:
        document = self.upload_document()
        raw_name = _stored_name(document)
        self.show("Vault Documents page", reverse("vault.documents"))
        self.show("Vault Documents items", reverse("vault.documents.items"))
        self.show("processing status", reverse("vault.photos.processing"), {"ids": str(document.pk)})

        process_image_upload(document.pk)

        self.assert_renamed_away(document, raw_name)
        self.assertTrue(_stored_name(document).endswith(".pdf"))
        self.assert_nothing_shown_names(raw_name)

    def test_the_lightbox_skips_a_pending_tile(self, *_mocks) -> None:
        document = self.upload_document()
        page = self.client.get(reverse("vault.documents")).content.decode()

        tile = page[page.index(f'id="document-tile-{document.pk}"') :].split(">", 1)[0]
        self.assertIn('data-url=""', tile)
        self.assertIn('data-processing="pending"', tile)
        self.assertIn(_LIGHTBOX_SELECTOR, page, "the lightbox opener no longer skips tiles still processing")

    def test_the_settled_tile_opens_the_converted_file(self, *_mocks) -> None:
        document = self.upload_document()

        process_image_upload(document.pk)

        document.refresh_from_db()
        [item] = self.status(document)["items"]
        self.assertTrue(item["url"].endswith(document.image.name))
        self.assert_served(item["url"])
        page = self.client.get(reverse("vault.documents")).content.decode()
        tile = page[page.index(f'id="document-tile-{document.pk}"') :].split(">", 1)[0]
        self.assertIn(f'data-url="{document.display_url}"', tile)
        self.assertNotIn("data-processing", tile)
        self.assert_served(document.display_url)


class OverlayUploadTests(_UploadLifecycle):
    """The aligner opens on the raw upload at once, so an overlay names it; a late load must reach the new file."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = baker.make(Pin, profile=self.user.profile)

    def add_overlay(self) -> tuple[MapImageOverlay, str]:
        with mock.patch(_ENQUEUE):
            response = self.client.post(
                reverse("pin.overlays", args=[self.pin.slug]),
                {
                    "corners": json.dumps(_CORNERS),
                    "name": "Sheet",
                    "image": SimpleUploadedFile("sheet.jpg", _jpeg_bytes(), "image/jpeg"),
                },
            )
        self.assertEqual(response.status_code, 200)
        return MapImageOverlay.objects.for_pin(self.pin).select_related("image").get(), response.content.decode()

    def test_an_uploaded_overlay_can_follow_its_photo_across_the_rename(self) -> None:
        overlay, _body = self.add_overlay()
        assert overlay.image is not None
        raw_name = _stored_name(overlay.image)
        payload = overlay.to_json()
        self.assertIn(raw_name, payload["url"])
        self.assertEqual(payload["image_link"], reverse("media.image", args=[overlay.image.uuid]))

        process_image_upload(overlay.image.pk)

        self.assert_renamed_away(overlay.image, raw_name)
        followed = self.client.get(payload["image_link"])
        self.assertEqual(followed.status_code, 302)
        self.assertEqual(followed["Location"], overlay.image.image.url)
        self.assert_served(followed["Location"])

    def test_the_manage_dialog_thumb_never_names_the_upload(self) -> None:
        overlay, body = self.add_overlay()
        assert overlay.image is not None
        self.assertNotIn(overlay.image.image.name, body)
        self.assertIn(f'src="{overlay.image_link}"', body)

        process_image_upload(overlay.image.pk)

        overlay.image.refresh_from_db()
        listing = self.client.get(reverse("pin.overlays", args=[self.pin.slug]))
        self.assertContains(listing, f'src="{overlay.image.image.url}"')

    def test_an_external_overlay_has_no_row_to_follow(self) -> None:
        overlay = MapImageOverlay(name="Sanborn", image_url="https://example.test/sheet.jpg")
        overlay.set_corners(_CORNERS)
        self.assertIsNone(overlay.to_json()["image_link"])
