"""Tests for georeferenced map image overlays.

Covers the model's corner handling, the three ways an image reaches an overlay
(upload / Media-gallery pick / external URL), the pin-vs-wiki permission split
those routes inherit from ``custom_layers``, and the corner-drag endpoint that
fires on every alignment nudge.

No real network access occurs - the gallery-pick path's download is mocked.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.markup.model import CustomLayer

_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]


def _png_bytes() -> bytes:
    """A tiny real PNG - the upload path sniffs magic bytes, not the filename."""
    from io import BytesIO

    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    return buffer.getvalue()


class CornerHandlingTests(SimpleTestCase):
    """The four corners are the whole georeferencing; partial writes aren't allowed."""

    def test_set_and_read_round_trip(self) -> None:
        overlay = MapImageOverlay()
        overlay.set_corners(_CORNERS)
        self.assertEqual(overlay.corners(), _CORNERS)

    def test_corners_are_stored_in_nw_ne_se_sw_order(self) -> None:
        overlay = MapImageOverlay()
        overlay.set_corners(_CORNERS)
        self.assertEqual([overlay.nw_latitude, overlay.nw_longitude], _CORNERS[0])
        self.assertEqual([overlay.se_latitude, overlay.se_longitude], _CORNERS[2])

    def test_wrong_corner_count_is_refused(self) -> None:
        """A partial write would leave some of the eight columns from a previous
        position, silently georeferencing the image somewhere nobody chose."""
        import pytest

        overlay = MapImageOverlay()
        with pytest.raises(ValueError, match="Expected 4 corners"):
            overlay.set_corners(_CORNERS[:3])

    def test_to_json_exposes_what_the_renderer_needs(self) -> None:
        overlay = MapImageOverlay(name="Sanborn 1897", image_url="https://example.test/sheet.jpg", opacity=55, locked=True)
        overlay.set_corners(_CORNERS)
        payload = overlay.to_json()
        self.assertEqual(payload["corners"], _CORNERS)
        self.assertEqual(payload["url"], "https://example.test/sheet.jpg")
        self.assertEqual(payload["opacity"], 55)
        self.assertTrue(payload["locked"])
        self.assertIsNone(payload["layer_uuid"])


class OverlayOwnerTests(TestCase):
    """Pin-scoped overlays are personal; wiki-scoped ones are community data."""

    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.user = baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        self.client.force_login(self.user)

    def _create(self, **extra):
        return self.client.post(
            reverse("pin.overlays", args=[self.pin.slug]),
            {"corners": json.dumps(_CORNERS), "image_url": "https://upload.wikimedia.org/sheet.jpg", **extra},
        )

    def test_an_external_url_overlay_is_created(self) -> None:
        response = self._create(name="Sanborn 1897")
        self.assertEqual(response.status_code, 200)
        overlay = MapImageOverlay.objects.for_pin(self.pin).get()
        self.assertEqual(overlay.name, "Sanborn 1897")
        self.assertEqual(overlay.corners(), _CORNERS)
        self.assertEqual(overlay.profile, self.user.profile)

    def test_an_uploaded_image_overlay_is_created(self) -> None:
        upload = SimpleUploadedFile("sheet.png", _png_bytes(), content_type="image/png")
        response = self.client.post(
            reverse("pin.overlays", args=[self.pin.slug]),
            {"corners": json.dumps(_CORNERS), "name": "Scan", "image": upload},
        )
        self.assertEqual(response.status_code, 200)
        overlay = MapImageOverlay.objects.for_pin(self.pin).get()
        self.assertIsNotNone(overlay.image_id)
        self.assertTrue(overlay.source_url)

    def test_a_media_gallery_pick_is_materialized_first(self) -> None:
        """Gallery items are transient, so referencing the provider URL would
        break the overlay the moment that URL rotted."""
        from urbanlens.dashboard.models.images.model import Image

        materialized = baker.make(Image, profile=self.user.profile, pin=self.pin)
        with patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=materialized) as mock_materialize:
            response = self.client.post(
                reverse("pin.overlays", args=[self.pin.slug]),
                {"corners": json.dumps(_CORNERS), "media_url": "https://tile.loc.gov/sanborn.jpg", "media_source": "library_of_congress"},
            )
        self.assertEqual(response.status_code, 200)
        mock_materialize.assert_called_once()
        self.assertEqual(MapImageOverlay.objects.for_pin(self.pin).get().image_id, materialized.pk)

    def test_an_existing_owned_photo_is_used_directly(self) -> None:
        """Picked from the dialog's own "this page's media" grid - already a
        real Image on this pin, so it must be reused as-is, not re-downloaded
        or duplicated the way a transient gallery item is."""
        from urbanlens.dashboard.models.images.model import Image

        existing = baker.make(Image, profile=self.user.profile, pin=self.pin)
        with patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item") as mock_materialize:
            response = self.client.post(
                reverse("pin.overlays", args=[self.pin.slug]),
                {"corners": json.dumps(_CORNERS), "image_id": str(existing.pk)},
            )
        self.assertEqual(response.status_code, 200)
        mock_materialize.assert_not_called()
        self.assertEqual(MapImageOverlay.objects.for_pin(self.pin).get().image_id, existing.pk)

    def test_another_pins_photo_cannot_be_picked(self) -> None:
        """A posted image_id is scoped to this owner - it isn't a free-form
        lookup across every Image in the database."""
        from urbanlens.dashboard.models.images.model import Image

        other_pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        someone_elses_photo = baker.make(Image, profile=other_pin.profile, pin=other_pin)
        response = self.client.post(
            reverse("pin.overlays", args=[self.pin.slug]),
            {"corners": json.dumps(_CORNERS), "image_id": str(someone_elses_photo.pk)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_another_users_pin_is_not_reachable(self) -> None:
        other_pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        response = self.client.post(
            reverse("pin.overlays", args=[other_pin.slug]),
            {"corners": json.dumps(_CORNERS), "image_url": "https://example.test/x.jpg"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(MapImageOverlay.objects.for_pin(other_pin).exists())

    def test_a_non_image_url_is_refused(self) -> None:
        """An external URL is loaded by the browser directly, so a PDF or TIFF
        here would be a silently blank overlay."""
        response = self._create(image_url="https://example.test/sheet.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_an_internal_url_is_refused(self) -> None:
        response = self._create(image_url="http://127.0.0.1/secret.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_no_image_at_all_is_refused(self) -> None:
        response = self.client.post(reverse("pin.overlays", args=[self.pin.slug]), {"corners": json.dumps(_CORNERS)})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_malformed_corners_are_refused(self) -> None:
        response = self._create(corners=json.dumps([[40.0, -74.0], [40.0, -74.0]]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_out_of_range_corners_are_refused(self) -> None:
        """Clamping would silently place the sheet somewhere the user didn't pick."""
        bad = [[200.0, -74.002], *(_CORNERS[1:])]
        response = self._create(corners=json.dumps(bad))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_the_per_map_limit_is_enforced(self) -> None:
        from urbanlens.dashboard.controllers.map_overlays import MAX_OVERLAYS_PER_MAP

        for index in range(MAX_OVERLAYS_PER_MAP):
            overlay = MapImageOverlay(parent_pin=self.pin, profile=self.user.profile, image_url=f"https://example.test/{index}.jpg")
            overlay.set_corners(_CORNERS)
            overlay.save()
        self._create()
        self.assertEqual(MapImageOverlay.objects.for_pin(self.pin).count(), MAX_OVERLAYS_PER_MAP)


class OverlayCornersEndpointTests(TestCase):
    """The drag endpoint fires on every alignment nudge, so it stays narrow."""

    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.user = baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        self.overlay = MapImageOverlay(parent_pin=self.pin, profile=self.user.profile, image_url="https://example.test/sheet.jpg")
        self.overlay.set_corners(_CORNERS)
        self.overlay.save()
        self.client.force_login(self.user)
        self.url = reverse("pin.overlays.corners", args=[self.pin.slug, self.overlay.uuid])

    def test_dragged_corners_are_persisted(self) -> None:
        moved = [[40.010, -74.012], *(_CORNERS[1:])]
        response = self.client.post(self.url, {"corners": json.dumps(moved)})
        self.assertEqual(response.status_code, 200)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.corners(), moved)

    def test_a_locked_overlay_refuses_to_move(self) -> None:
        """The handles are hidden client-side, but a stale tab must not move it."""
        MapImageOverlay.objects.filter(pk=self.overlay.pk).update(locked=True)
        response = self.client.post(self.url, {"corners": json.dumps([[41.0, -75.0], *(_CORNERS[1:])])})
        self.assertEqual(response.status_code, 409)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.corners(), _CORNERS)

    def test_malformed_corners_are_rejected_without_writing(self) -> None:
        response = self.client.post(self.url, {"corners": "not json"})
        self.assertEqual(response.status_code, 400)
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.corners(), _CORNERS)

    def test_another_users_overlay_is_not_reachable(self) -> None:
        other_pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        response = self.client.post(
            reverse("pin.overlays.corners", args=[other_pin.slug, self.overlay.uuid]),
            {"corners": json.dumps(_CORNERS)},
        )
        self.assertEqual(response.status_code, 404)


class OverlaySettingsTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = Client()
        self.user = baker.make(User)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile)
        self.overlay = MapImageOverlay(parent_pin=self.pin, profile=self.user.profile, image_url="https://example.test/sheet.jpg")
        self.overlay.set_corners(_CORNERS)
        self.overlay.save()
        self.client.force_login(self.user)
        self.url = reverse("pin.overlays.edit", args=[self.pin.slug, self.overlay.uuid])

    def test_opacity_and_lock_are_updated(self) -> None:
        self.client.post(self.url, {"name": "Sheet 12", "opacity": "35", "locked": "1"})
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.name, "Sheet 12")
        self.assertEqual(self.overlay.opacity, 35)
        self.assertTrue(self.overlay.locked)

    def test_opacity_is_clamped_to_a_percentage(self) -> None:
        self.client.post(self.url, {"opacity": "9000"})
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.opacity, 100)

    def test_an_overlay_can_join_one_of_its_own_maps_layers(self) -> None:
        layer = baker.make(CustomLayer, parent_pin=self.pin, profile=self.user.profile, name="Historic")
        self.client.post(self.url, {"layer": str(layer.uuid)})
        self.overlay.refresh_from_db()
        self.assertEqual(self.overlay.layer_id, layer.pk)

    def test_it_cannot_join_another_maps_layer(self) -> None:
        """A posted uuid must not be able to attach this overlay elsewhere."""
        foreign_pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)
        foreign_layer = baker.make(CustomLayer, parent_pin=foreign_pin, profile=foreign_pin.profile, name="Theirs")
        self.client.post(self.url, {"layer": str(foreign_layer.uuid)})
        self.overlay.refresh_from_db()
        self.assertIsNone(self.overlay.layer_id)

    def test_delete_removes_the_overlay_but_keeps_the_image(self) -> None:
        from urbanlens.dashboard.models.images.model import Image

        image = baker.make(Image, profile=self.user.profile, pin=self.pin)
        MapImageOverlay.objects.filter(pk=self.overlay.pk).update(image=image)
        self.client.delete(reverse("pin.overlays.delete", args=[self.pin.slug, self.overlay.uuid]))
        self.assertFalse(MapImageOverlay.objects.filter(pk=self.overlay.pk).exists())
        self.assertTrue(Image.objects.filter(pk=image.pk).exists())


class RenderableQuerySetTests(TestCase):
    """An overlay whose image was deleted keeps its georeferencing but can't draw."""

    def test_an_overlay_with_no_image_is_excluded(self) -> None:
        user = baker.make(User)
        pin = baker.make_recipe("dashboard.pin", profile=user.profile)
        drawable = MapImageOverlay(parent_pin=pin, profile=user.profile, image_url="https://example.test/a.jpg")
        drawable.set_corners(_CORNERS)
        drawable.save()
        orphan = MapImageOverlay(parent_pin=pin, profile=user.profile)
        orphan.set_corners(_CORNERS)
        orphan.save()

        self.assertEqual([overlay.pk for overlay in MapImageOverlay.objects.for_pin(pin).renderable()], [drawable.pk])
