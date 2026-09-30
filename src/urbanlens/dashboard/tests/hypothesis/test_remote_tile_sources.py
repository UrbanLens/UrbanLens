"""An imported overlay's foreign tile template: a recognised sheet is rebuilt onto this site's route, and any other
host's tiles are drawn through this site and kept (Jess, 2026-09-30, rulings item 16)."""

from __future__ import annotations

from io import BytesIO
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.remote_tiles.model import RemoteTile, RemoteTileSource
from urbanlens.dashboard.services.import_export import import_data
from urbanlens.dashboard.services.map.image_overlays import historical_tile_template
from urbanlens.dashboard.services.map.remote_tiles import (
    MAX_TILE_ZOOM,
    TileDownload,
    kept_tile_template,
    pending_marker,
    template_digest,
)

_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]
_PUBLIC_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]
_SHEET = "5b0e8a4c-2d1f-4a57-9c3e-1f2a3b4c5d6e"
_FOREIGN = "https://{s}.tiles.example/sanborn/{z}/{x}/{y}.png"
_DOWNLOAD = "urbanlens.dashboard.controllers.remote_tiles.download_tile"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-remote-tiles-")


def _png(mode: str = "RGBA") -> bytes:
    from PIL import Image as PILImage

    buffer = BytesIO()
    PILImage.new(mode, (256, 256), (200, 0, 0, 120) if mode == "RGBA" else "red").save(buffer, format="PNG")
    return buffer.getvalue()


class _ImportCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.data_dir = tempfile.mkdtemp(prefix="ul_tile_import_")
        self.addCleanup(shutil.rmtree, self.data_dir, ignore_errors=True)
        self.ctx = import_data.ImportContext(
            profile=self.profile,
            data_dir=self.data_dir,
            result=import_data.ImportResult(),
            pin_uuid_map={str(self.pin.uuid): self.pin.pk},
            label_uuid_map={},
        )

    def _import(self, template: str) -> MapImageOverlay | None:
        row = {
            "name": "Sanborn",
            "corners": _CORNERS,
            "target_type": "pin",
            "target_uuid": str(self.pin.uuid),
            "tile_url_template": template,
        }
        with patch("socket.getaddrinfo", return_value=_PUBLIC_DNS_RESULT):
            import_data.MapAnnotationsImport()._import_overlay(row, self.ctx)
        return MapImageOverlay.objects.for_pin(self.pin).first()


class RecognisedSheetTests(_ImportCase):
    def test_another_deployments_sheet_is_rebuilt_onto_this_sites_route(self) -> None:
        overlay = self._import(f"https://staging.urbanlens.org{historical_tile_template(_SHEET)}")

        assert overlay is not None
        self.assertEqual(overlay.tile_url_template, historical_tile_template(_SHEET))
        self.assertFalse(RemoteTileSource.objects.exists())

    def test_redatas_own_tile_address_is_rebuilt_onto_this_sites_route(self) -> None:
        overlay = self._import(f"https://redata.example/api/v1/maps/georeferences/{_SHEET}/tiles/{{z}}/{{x}}/{{y}}.png")

        assert overlay is not None
        self.assertEqual(overlay.tile_url_template, historical_tile_template(_SHEET))

    def test_a_sheet_address_with_a_query_is_not_taken_for_a_sheet(self) -> None:
        overlay = self._import(f"https://tracker.example{historical_tile_template(_SHEET)}?id=me")

        assert overlay is not None
        self.assertNotEqual(overlay.tile_url_template, historical_tile_template(_SHEET))
        self.assertEqual(
            RemoteTileSource.objects.get().template, f"https://tracker.example{historical_tile_template(_SHEET)}?id=me"
        )


class ForeignTemplateImportTests(_ImportCase):
    def test_another_hosts_tiles_are_drawn_through_this_site(self) -> None:
        overlay = self._import(_FOREIGN)

        assert overlay is not None
        source = RemoteTileSource.objects.get()
        self.assertEqual((source.template, source.provider), (_FOREIGN, "import"))
        self.assertEqual(
            overlay.tile_url_template,
            reverse("map.remote_tiles", args=[template_digest(_FOREIGN), 0, 0, 0]).replace(
                "/0/0/0.png", "/{z}/{x}/{y}.png"
            ),
        )
        self.assertNotIn("tiles.example", str(overlay.to_json()))

    def test_an_internal_host_is_refused(self) -> None:
        self.assertIsNone(self._import("http://127.0.0.1/{z}/{x}/{y}.png"))
        self.assertFalse(RemoteTileSource.objects.exists())

    def test_a_template_without_every_coordinate_is_refused(self) -> None:
        self.assertIsNone(self._import("https://tiles.example/{z}/{x}.png"))

    def test_an_unknown_placeholder_is_refused(self) -> None:
        self.assertIsNone(self._import("https://tiles.example/{z}/{x}/{y}.png?key={apikey}"))

    def test_the_round_trip_exports_the_hosts_template_and_reimports_to_the_same_source(self) -> None:
        from urbanlens.dashboard.services.import_export.export import MapAnnotationsExport

        overlay = self._import(_FOREIGN)
        assert overlay is not None
        row = MapAnnotationsExport()._overlay_row(overlay, self.data_dir)
        self.assertEqual(row["tile_url_template"], _FOREIGN)

        overlay.delete()
        again = self._import(row["tile_url_template"])

        assert again is not None
        self.assertEqual(again.tile_url_template, overlay.tile_url_template)
        self.assertEqual(RemoteTileSource.objects.count(), 1)

    def test_this_sites_own_kept_template_is_accepted_only_for_a_source_it_has(self) -> None:
        known = kept_tile_template(_FOREIGN, provider="import")
        unknown = known.replace(template_digest(_FOREIGN), "a" * 64)

        self.assertIsNone(self._import(unknown))
        overlay = self._import(known)

        assert overlay is not None
        self.assertEqual(overlay.tile_url_template, known)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class KeptTileEndpointTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(baker.make(User))
        self.template = f"https://{{s}}.tiles.example/{self.id().rsplit('.', 1)[-1]}/{{z}}/{{x}}/{{y}}.png"
        kept_tile_template(self.template, provider="import")
        self.source = RemoteTileSource.objects.get()
        self.digest = self.source.template_digest
        self.url = reverse("map.remote_tiles", args=[self.digest, 3, 2, 5])
        self.addCleanup(cache.delete, pending_marker(self.digest, 3, 2, 5))

    def _first_request(self, download: TileDownload | None) -> tuple[int, MagicMock, MagicMock]:
        with patch(_DOWNLOAD, return_value=download) as fetch, patch(_ENQUEUE) as enqueue:
            response = self.client.get(self.url)
        return response.status_code, fetch, enqueue

    def _render(self, enqueue: MagicMock) -> bool:
        from urbanlens.dashboard.tasks import render_remote_tile

        _task, tile_id, descriptor = enqueue.call_args.args
        return render_remote_tile(tile_id, descriptor)

    def test_a_template_this_site_never_recorded_is_refused_without_fetching(self) -> None:
        with patch(_DOWNLOAD) as fetch:
            response = self.client.get(reverse("map.remote_tiles", args=["b" * 64, 3, 2, 5]))

        self.assertEqual(response.status_code, 404)
        fetch.assert_not_called()

    def test_a_coordinate_outside_the_grid_is_refused_without_fetching_or_a_row(self) -> None:
        with patch(_DOWNLOAD) as fetch:
            for z, x, y in ((3, 8, 0), (3, 0, 8), (MAX_TILE_ZOOM + 1, 0, 0)):
                self.assertEqual(
                    self.client.get(reverse("map.remote_tiles", args=[self.digest, z, x, y])).status_code, 404
                )

        fetch.assert_not_called()
        self.assertFalse(RemoteTile.objects.exists())

    def test_the_first_request_fetches_the_hosts_tile_and_asks_the_map_to_retry(self) -> None:
        status, fetch, enqueue = self._first_request(TileDownload(200, _png(), "image/png"))

        self.assertEqual(status, 503)
        fetch.assert_called_once_with(self.template.replace("{s}", "a").replace("{z}/{x}/{y}", "3/2/5"))
        enqueue.assert_called_once()

    def test_requests_while_the_tile_is_being_kept_do_not_fetch_again(self) -> None:
        self._first_request(TileDownload(200, _png(), "image/png"))
        with patch(_DOWNLOAD) as fetch:
            for _ in range(3):
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 503)
                self.assertTrue(response.has_header("Retry-After"))

        fetch.assert_not_called()

    def test_once_kept_the_tile_is_served_re_encoded_without_the_host(self) -> None:
        from PIL import Image as PILImage

        _status, _fetch, enqueue = self._first_request(TileDownload(200, _png(), "image/png"))
        self.assertTrue(self._render(enqueue))

        with patch(_DOWNLOAD, side_effect=AssertionError("fetched the host again")):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response["Cache-Control"])
        tile = RemoteTile.objects.get()
        self.assertTrue(tile.file.name.startswith(f"remote_tiles/{self.digest[:2]}/{self.digest}/3/2/"))
        self.assertEqual(tile.content_type, "image/png")
        with PILImage.open(BytesIO(b"".join(response.streaming_content))) as served:
            self.assertEqual(served.mode, "RGBA", "an overlay's transparency must survive the re-encode")
        self.assertEqual(RemoteTileSource.objects.get().kept_tiles, 1)

    def test_a_tile_the_host_has_not_got_is_remembered(self) -> None:
        status, _fetch, enqueue = self._first_request(TileDownload(404))

        self.assertEqual(status, 404)
        enqueue.assert_not_called()
        with patch(_DOWNLOAD) as fetch:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        fetch.assert_not_called()
        self.assertTrue(RemoteTile.objects.get().absent)

    def test_bytes_that_are_not_an_image_are_a_failure_not_a_tile(self) -> None:
        status, _fetch, enqueue = self._first_request(TileDownload(200, b"<html>hello</html>", "image/png"))

        self.assertEqual(status, 404)
        enqueue.assert_not_called()
        tile = RemoteTile.objects.get()
        self.assertEqual((tile.file.name, tile.failed_attempts), ("", 1))
        with patch(_DOWNLOAD) as fetch:
            self.assertEqual(self.client.get(self.url).status_code, 404)
        fetch.assert_not_called()

    def test_a_request_over_the_download_slots_is_told_to_retry_without_a_row_or_a_failure(self) -> None:
        from urbanlens.dashboard.services.apis.request_upstreams import RemoteTileUpstream

        semaphore = RemoteTileUpstream.semaphore()
        held = 0
        while semaphore.acquire(blocking=False):
            held += 1
        try:
            with patch(_DOWNLOAD) as fetch:
                response = self.client.get(self.url)
        finally:
            for _ in range(held):
                semaphore.release()

        self.assertEqual(response.status_code, 503)
        fetch.assert_not_called()
        self.assertFalse(RemoteTile.objects.exists())

    def test_a_source_at_its_cap_fetches_no_new_tiles(self) -> None:
        from urbanlens.dashboard.services.map.remote_tiles import MAX_KEPT_TILES_PER_SOURCE

        RemoteTileSource.objects.filter(pk=self.source.pk).update(kept_tiles=MAX_KEPT_TILES_PER_SOURCE)
        status, fetch, _enqueue = self._first_request(TileDownload(200, _png(), "image/png"))

        self.assertEqual(status, 404)
        fetch.assert_not_called()

    def test_a_render_that_lands_after_the_cap_was_reached_keeps_nothing(self) -> None:
        from urbanlens.dashboard.services.map.remote_tiles import MAX_KEPT_TILES_PER_SOURCE

        _status, _fetch, enqueue = self._first_request(TileDownload(200, _png(), "image/png"))
        RemoteTileSource.objects.filter(pk=self.source.pk).update(kept_tiles=MAX_KEPT_TILES_PER_SOURCE)

        self.assertFalse(self._render(enqueue))
        tile = RemoteTile.objects.get()
        self.assertEqual((tile.file.name, tile.failed_attempts), ("", 1))

    def test_signed_out_visitors_get_nothing(self) -> None:
        self.client.logout()
        with patch(_DOWNLOAD) as fetch:
            response = self.client.get(self.url)

        self.assertNotEqual(response.status_code, 200)
        fetch.assert_not_called()

    def test_a_kept_tile_passes_the_media_gate(self) -> None:
        from urbanlens.dashboard.services.media.access import authorize_media

        _status, _fetch, enqueue = self._first_request(TileDownload(200, _png(), "image/png"))
        self._render(enqueue)

        self.assertTrue(authorize_media(baker.make(User).profile, RemoteTile.objects.get().file.name))
