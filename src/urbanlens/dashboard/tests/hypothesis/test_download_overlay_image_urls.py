"""Downloading the image of an overlay that still draws only from an external ``image_url``, so migration
0033_v0_8_0_indexes can run.

The command runs against a database stopped at 0032_v0_8_0, where the column exists but the model no longer declares it.
The command tests give the table that shape inside their own transaction; the last test walks the real migrations.
"""

from __future__ import annotations

from io import BytesIO, StringIO
import re
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command, get_commands
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import ProgrammingError
from django.test import TransactionTestCase
from model_bakery import baker
from PIL import Image as PILImage
import pytest
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay

_COMMAND = "download_overlay_image_urls"
_URL = "https://maps.example.org/sanborn-1911.png"
_CORNERS = [[40.002, -74.002], [40.002, -74.000], [40.000, -74.000], [40.000, -74.002]]
_PUBLIC_DNS_RESULT = [(2, 1, 6, "", ("93.184.216.34", 0))]
_FETCH = "urbanlens.dashboard.services.media.media_materialize.fetch_with_revalidated_redirects"
_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_TABLE = MapImageOverlay._meta.db_table
_BEFORE_REFUSAL = ("dashboard", "0032_v0_8_0")


def _png() -> bytes:
    buffer = BytesIO()
    PILImage.new("RGB", (4, 4), "white").save(buffer, "PNG")
    return buffer.getvalue()


def _download(content: bytes | None = None) -> MagicMock:
    response = MagicMock()
    response.raw.read.return_value = _png() if content is None else content
    return MagicMock(return_value=response)


def _run(fetch: MagicMock, *flags: str) -> str:
    """Run the command with the network and the upload pipeline's queue stubbed out."""
    out = StringIO()
    with (
        patch("socket.getaddrinfo", return_value=_PUBLIC_DNS_RESULT),
        patch(_FETCH, fetch),
        patch(_ENQUEUE),
    ):
        call_command(_COMMAND, *flags, stdout=out, stderr=out)
    return out.getvalue()


def _image_url(pk: int) -> str:
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT "image_url" FROM "{_TABLE}" WHERE "id" = %s', [pk])
        return cursor.fetchone()[0]


class _UrlOnlyOverlayCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute(f'ALTER TABLE "{_TABLE}" DROP CONSTRAINT "db_overlay_one_source"')
            cursor.execute(f"""ALTER TABLE "{_TABLE}" ADD COLUMN "image_url" varchar(1000) NOT NULL DEFAULT ''""")
        self.profile = baker.make(User).profile
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile)
        self.fetch = _download()

    def _overlay(self, url: str = _URL, **fields) -> MapImageOverlay:
        owner = fields.pop("owner", {"parent_pin": self.pin})
        overlay = MapImageOverlay(name="Sanborn 1911", profile=self.profile, **owner, **fields)
        overlay.set_corners(_CORNERS)
        overlay.save()
        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE "{_TABLE}" SET "image_url" = %s WHERE "id" = %s', [url, overlay.pk])
        return overlay

    def _assert_left_for_a_human(self, overlay: MapImageOverlay, url: str = _URL) -> None:
        with self.assertRaises(CommandError) as raised:
            _run(self.fetch)
        self.assertIn(str(overlay.pk), str(raised.exception), "the operator isn't told which overlay is left")
        self.assertTrue(MapImageOverlay.objects.filter(pk=overlay.pk, image__isnull=True).exists())
        self.assertEqual(_image_url(overlay.pk), url)


class DownloadingAnOverlayImageTests(_UrlOnlyOverlayCase):
    def test_the_image_is_downloaded_into_the_overlay_and_the_url_cleared(self) -> None:
        overlay = self._overlay()

        _run(self.fetch)

        image = Image.objects.get(map_overlays=overlay)
        self.assertEqual(self.fetch.call_args.args[0], _URL)
        self.assertEqual(image.pin_id, self.pin.pk)
        self.assertEqual(image.source_url, _URL, "the image forgot where it came from")
        self.assertEqual(_image_url(overlay.pk), "")

    def test_a_wiki_overlay_stores_its_image_on_the_wiki(self) -> None:
        wiki = baker.make_recipe("dashboard.wiki")
        overlay = self._overlay(owner={"parent_wiki": wiki})

        _run(self.fetch)

        self.assertEqual(Image.objects.get(map_overlays=overlay).wiki_id, wiki.pk)

    def test_an_overlay_that_already_has_an_image_is_left_alone(self) -> None:
        image = baker.make(Image, profile=self.profile, pin=self.pin)
        overlay = self._overlay(image=image)

        _run(self.fetch)

        self.fetch.assert_not_called()
        overlay.refresh_from_db()
        self.assertEqual(overlay.image_id, image.pk)


class NothingIsDeletedTests(_UrlOnlyOverlayCase):
    """A URL that cannot become a stored image leaves the overlay, URL and all, for a human (Jess, 2026-09-29)."""

    def test_a_failed_download_deletes_nothing(self) -> None:
        overlay = self._overlay()
        self.fetch.side_effect = requests.ConnectionError("gone")

        self._assert_left_for_a_human(overlay)

        self.fetch.assert_called_once()
        self.assertFalse(Image.objects.filter(source_url=_URL).exists())

    def test_an_internal_url_is_never_fetched(self) -> None:
        overlay = self._overlay(url="http://127.0.0.1/secret.png")

        self._assert_left_for_a_human(overlay, url="http://127.0.0.1/secret.png")

        self.fetch.assert_not_called()

    def test_a_page_returned_in_place_of_the_image_is_not_linked(self) -> None:
        """The pipeline deletes a file it can't open and the overlay's image FK cascades, so linking it would delete
        the overlay once the file was processed."""
        overlay = self._overlay()
        self.fetch = _download(b"<!doctype html><title>Hotlinking not permitted</title>")

        self._assert_left_for_a_human(overlay)

        self.fetch.assert_called_once()

    def test_an_overlay_with_no_url_either_is_reported_not_deleted(self) -> None:
        overlay = self._overlay(url="")

        self._assert_left_for_a_human(overlay, url="")

    def test_a_dry_run_downloads_and_changes_nothing(self) -> None:
        overlay = self._overlay()

        _run(self.fetch, "--dry-run")

        self.fetch.assert_not_called()
        self.assertTrue(MapImageOverlay.objects.filter(pk=overlay.pk, image__isnull=True).exists())
        self.assertEqual(_image_url(overlay.pk), _URL)


class AfterTheColumnIsGoneTests(TestCase):
    def test_it_is_a_clean_no_op(self) -> None:
        fetch = _download()

        _run(fetch)

        fetch.assert_not_called()


class TheMigrationNamesTheCommandTests(TestCase):
    def test_the_command_the_refusal_tells_the_operator_to_run_exists(self) -> None:
        import importlib

        module = importlib.import_module("urbanlens.dashboard.migrations.0033_v0_8_0_indexes")
        overlays = MagicMock()
        overlays.objects.filter.return_value.values_list.return_value = [7]
        registry = MagicMock()
        registry.get_model.return_value = overlays

        with self.assertRaises(RuntimeError) as raised:
            module._0096_refuse_to_drop_url_only_overlays(registry, None)

        named = re.search(r"manage\.py (\w+)", str(raised.exception))
        if named is None:
            self.fail("the refusal does not say what to run")
        self.assertIn(named.group(1), get_commands())


class TheOperatorsPathTests(TransactionTestCase):
    """0033 stops, the command runs against the 0032 schema with the current code, then 0033 applies and the
    upload pipeline processes the image, as ``requeue_stalled_pending_uploads`` would have it do."""

    @staticmethod
    def _migrate(targets: list[tuple[str, str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)

    def _restore(self, leaves: list[tuple[str, str]]) -> None:
        with connection.cursor() as cursor:
            if any(
                column.name == "image_url" for column in connection.introspection.get_table_description(cursor, _TABLE)
            ):
                cursor.execute(f"""DELETE FROM "{_TABLE}" WHERE "image_id" IS NULL AND "tile_url_template" = ''""")
        self._migrate(leaves)

    @pytest.mark.xfail(
        strict=True,
        raises=ProgrammingError,
        reason="P242: the command runs today's models on the 0032 schema, which lacks columns added since",
    )
    def test_the_migration_proceeds_once_the_command_has_run(self) -> None:
        from urbanlens.dashboard.tasks import process_image_upload

        profile = baker.make(User).profile
        pin = baker.make_recipe("dashboard.pin", profile=profile)
        leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self._migrate([_BEFORE_REFUSAL])
        self.addCleanup(self._restore, leaves)
        historical = (
            MigrationExecutor(connection)
            .loader.project_state(_BEFORE_REFUSAL)
            .apps.get_model("dashboard", "MapImageOverlay")
        )
        corners = {
            f"{corner}_{axis}": value
            for corner, pair in zip(("nw", "ne", "se", "sw"), _CORNERS, strict=True)
            for axis, value in zip(("latitude", "longitude"), pair, strict=True)
        }
        pk = historical.objects.create(
            name="Sanborn 1911", image_url=_URL, parent_pin_id=pin.pk, profile_id=profile.pk, **corners
        ).pk

        with self.assertRaisesRegex(RuntimeError, _COMMAND):
            self._migrate(leaves)
        _run(_download())
        self._migrate(leaves)
        image = Image.objects.get(map_overlays__pk=pk)
        with patch("urbanlens.dashboard.tasks.update_task_progress"):
            processed = process_image_upload(image.pk)

        self.assertTrue(processed)
        image.refresh_from_db()
        self.assertFalse(image.pending_scan)
        self.assertEqual(image.source_url, _URL)
