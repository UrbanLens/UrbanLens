"""Every way an overlay is created obeys the same rules as the live form (N29 G3-1, G3-11, G6-22).

The archive importer kept a pasted ``image_url`` and handed it to every viewer's browser, and never looked at the
per-map cap, while the form downloads the image and refuses a thirteenth overlay.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.services.import_export import import_data

_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]
_PUBLIC_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]
_MATERIALIZE = "urbanlens.dashboard.services.media.media_materialize.materialize_media_item"


def _tile_overlay(**owner) -> MapImageOverlay:
    overlay = MapImageOverlay(tile_url_template="/map/historical/x/{z}/{x}/{y}.png", **owner)
    overlay.set_corners(_CORNERS)
    overlay.save()
    return overlay


class _ImportCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.data_dir = tempfile.mkdtemp(prefix="ul_overlay_import_")
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.result = import_data.ImportResult()
        self.ctx = import_data.ImportContext(
            profile=self.profile,
            data_dir=self.data_dir,
            result=self.result,
            pin_uuid_map={str(self.pin.uuid): self.pin.pk},
            label_uuid_map={},
        )

    def _row(self, **extra) -> dict:
        return {
            "name": "Sanborn",
            "corners": _CORNERS,
            "target_type": "pin",
            "target_uuid": str(self.pin.uuid),
            **extra,
        }


class AnImportedOverlayUrlTests(_ImportCase):
    def test_the_url_is_downloaded_not_handed_to_viewers(self) -> None:
        materialized = baker.make(Image, profile=self.profile, pin=self.pin)
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_DNS_RESULT),
            patch(_MATERIALIZE, return_value=materialized) as materialize,
        ):
            import_data.MapAnnotationsImport()._import_overlay(
                self._row(image_url="https://tracker.example/beacon.jpg"), self.ctx
            )

        materialize.assert_called_once()
        self.assertEqual(materialize.call_args.kwargs["url"], "https://tracker.example/beacon.jpg")
        overlay = MapImageOverlay.objects.for_pin(self.pin).get()
        self.assertEqual(overlay.image_id, materialized.pk)
        self.assertNotIn("tracker.example", str(overlay.to_json()))

    def test_an_internal_url_is_never_fetched(self) -> None:
        with patch(_MATERIALIZE) as materialize:
            import_data.MapAnnotationsImport()._import_overlay(
                self._row(image_url="http://127.0.0.1/secret.jpg"), self.ctx
            )

        materialize.assert_not_called()
        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_a_failed_download_skips_the_row(self) -> None:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError

        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_DNS_RESULT),
            patch(_MATERIALIZE, side_effect=MaterializeError("gone")),
        ):
            import_data.MapAnnotationsImport()._import_overlay(
                self._row(image_url="https://example.test/sheet.jpg"), self.ctx
            )

        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())
        self.assertEqual(self.result.skipped.get("map_overlays"), 1)


class AnImportedTileOverlayTests(_ImportCase):
    def test_a_historical_tile_overlay_survives_the_round_trip(self) -> None:
        from urbanlens.dashboard.services.map.image_overlays import historical_tile_template

        template = historical_tile_template("5b0e8a4c-2d1f-4a57-9c3e-1f2a3b4c5d6e")
        import_data.MapAnnotationsImport()._import_overlay(self._row(tile_url_template=template, locked=True), self.ctx)

        overlay = MapImageOverlay.objects.for_pin(self.pin).get()
        self.assertEqual(overlay.tile_url_template, template)
        self.assertIsNone(overlay.image_id)

    def test_a_foreign_tile_template_is_refused(self) -> None:
        import_data.MapAnnotationsImport()._import_overlay(
            self._row(tile_url_template="https://tracker.example/{z}/{x}/{y}.png"), self.ctx
        )

        self.assertFalse(MapImageOverlay.objects.for_pin(self.pin).exists())

    def test_the_export_carries_the_tile_template(self) -> None:
        from urbanlens.dashboard.services.import_export.export import MapAnnotationsExport

        overlay = _tile_overlay(parent_pin=self.pin, profile=self.profile)
        row = MapAnnotationsExport()._overlay_row(overlay, self.data_dir)
        self.assertEqual(row["tile_url_template"], overlay.tile_url_template)
        self.assertNotIn("image_url", row)


class TheOverlayCapTests(_ImportCase):
    def test_an_import_stops_at_the_per_map_cap(self) -> None:
        from urbanlens.dashboard.services.map.image_overlays import MAX_OVERLAYS_PER_MAP

        for _ in range(MAX_OVERLAYS_PER_MAP):
            _tile_overlay(parent_pin=self.pin, profile=self.profile)
        materialized = baker.make(Image, profile=self.profile, pin=self.pin)
        with (
            patch("socket.getaddrinfo", return_value=_PUBLIC_DNS_RESULT),
            patch(_MATERIALIZE, return_value=materialized) as materialize,
        ):
            import_data.MapAnnotationsImport()._import_overlay(
                self._row(image_url="https://example.test/13.jpg"), self.ctx
            )

        self.assertEqual(MapImageOverlay.objects.for_pin(self.pin).count(), MAX_OVERLAYS_PER_MAP)
        materialize.assert_not_called()
        self.assertEqual(self.result.skipped.get("map_overlays"), 1)

    def test_the_wiki_cap_counts_overlays_this_creator_cannot_see(self) -> None:
        """The views count a viewer-filtered queryset on a wiki, so the service must count the whole map."""
        from urbanlens.dashboard.services.map.image_overlays import (
            MAX_OVERLAYS_PER_MAP,
            OverlayLimitError,
            create_overlay,
        )

        wiki = baker.make_recipe("dashboard.wiki")
        stranger = baker.make(User).profile
        for _ in range(MAX_OVERLAYS_PER_MAP):
            _tile_overlay(parent_wiki=wiki, profile=stranger)
        image = baker.make(Image, profile=self.profile, wiki=wiki)

        with self.assertRaises(OverlayLimitError):
            create_overlay(wiki, profile=self.profile, corners=_CORNERS, image=image)
        self.assertEqual(MapImageOverlay.objects.for_wiki(wiki).count(), MAX_OVERLAYS_PER_MAP)


class TheOverlayModelTests(TestCase):
    def test_there_is_no_external_url_column(self) -> None:
        self.assertNotIn("image_url", {field.name for field in MapImageOverlay._meta.get_fields()})

    def test_an_overlay_with_nothing_to_draw_cannot_be_stored(self) -> None:
        pin = baker.make_recipe("dashboard.pin")
        overlay = MapImageOverlay(parent_pin=pin, profile=pin.profile)
        overlay.set_corners(_CORNERS)
        with self.assertRaises(IntegrityError):
            overlay.save()


class UrlOnlyOverlayMigrationTests(TestCase):
    """Migration 0096 never deletes an overlay: a pasted image is downloaded, not dropped (Jess, 2026-09-29)."""

    @staticmethod
    def _migrate(registry) -> None:
        import importlib

        module = importlib.import_module("urbanlens.dashboard.migrations.0096_drop_overlays_without_stored_source")
        module.refuse_to_drop_url_only_overlays(registry, None)

    def test_it_deletes_nothing_when_every_overlay_has_a_source(self) -> None:
        from django.apps import apps

        from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

        before = MapImageOverlay.objects.count()
        self._migrate(apps)
        self.assertEqual(MapImageOverlay.objects.count(), before)

    def test_it_stops_rather_than_delete_an_overlay_without_a_source(self) -> None:
        """The current schema can't hold such a row, so the historical model is stood in for."""
        overlays = MagicMock()
        overlays.objects.filter.return_value.values_list.return_value = [7]
        registry = MagicMock()
        registry.get_model.return_value = overlays

        with self.assertRaises(RuntimeError):
            self._migrate(registry)
        overlays.objects.filter.return_value.delete.assert_not_called()
