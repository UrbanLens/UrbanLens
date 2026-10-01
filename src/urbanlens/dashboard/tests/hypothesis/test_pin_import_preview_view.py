"""Tests for PinController.parse_for_preview - the import wizard's file-parsing step."""

from __future__ import annotations

import io
import json
from unittest import mock
import zipfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.tasks import finish_import_preview_task, parse_import_preview_task

_KML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <Placemark>
    <name>Test Spot</name>
    <Point><coordinates>-73.9251,41.7003,0</coordinates></Point>
  </Placemark>
</Document>
</kml>
"""


def _kmz_bytes(inner_filename: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_filename, _KML_TEMPLATE)
    return buf.getvalue()


def _read_preview(test: TestCase, uploads: list[SimpleUploadedFile]) -> dict:
    """Post an upload and let the preview's tasks run, returning what the dialog receives."""
    with tasks_run_inline(parse_import_preview_task, finish_import_preview_task):
        response = test.client.post(reverse("pin.import.preview"), {"upload_files": uploads})
    test.assertEqual(response.status_code, 202, response.content)
    state = test.client.get(response.json()["status_url"]).json()
    test.assertEqual(state["status"], "done", state)
    return state["result"]


class ParseForPreviewStemTests(TestCase):
    """PinController.parse_for_preview - suggested list/category name per file."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _post(self, filename: str, data: bytes) -> list[dict]:
        upload = SimpleUploadedFile(filename, data, content_type="application/octet-stream")
        return _read_preview(self, [upload])["lists"]

    def test_kmz_wrapping_doc_kml_uses_the_outer_filename(self) -> None:
        """The exact reported bug: Google Takeout/My Maps KMZ always names its
        single internal file "doc.kml" - the suggested stem must come from
        the .kmz the user actually uploaded, not that generic inner name."""
        lists = self._post("My Saved Places.kmz", _kmz_bytes("doc.kml"))
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["stem"], "My Saved Places")

    def test_plain_kml_upload_uses_its_own_filename(self) -> None:
        """Sanity baseline: a non-archive upload is completely unaffected."""
        upload = SimpleUploadedFile(
            "Urbex Sites.kml", _KML_TEMPLATE.encode(), content_type="application/vnd.google-earth.kml+xml"
        )
        lists = _read_preview(self, [upload])["lists"]
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["stem"], "Urbex Sites")

    def test_zip_with_a_meaningfully_named_single_file_keeps_its_own_name(self) -> None:
        """The substitution is scoped to the literal "doc" placeholder - a ZIP
        wrapping one real, deliberately-named file must not be renamed to the
        outer archive's filename instead."""
        lists = self._post("archive.zip", _kmz_bytes("Meaningful Export.kml"))
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["stem"], "Meaningful Export")

    def test_zip_with_multiple_files_keeps_each_own_name(self) -> None:
        """The special-case only applies to a single-entry archive - multiple
        distinctly-named files inside a ZIP are unaffected, "doc" or not."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("doc.kml", _KML_TEMPLATE)
            zf.writestr("Other Sites.kml", _KML_TEMPLATE)
        lists = self._post("Takeout.zip", buf.getvalue())
        stems = sorted(entry["stem"] for entry in lists)
        self.assertEqual(stems, ["Other Sites", "doc"])

    def test_preview_works_with_no_google_api_key_configured(self) -> None:
        """Regression test: this endpoint used to build a `GoogleMapsGateway()`
        unconditionally to reach its file-parsing methods, and that gateway
        raised ValueError the instant it was constructed with no API key -
        crashing every import preview (KML/GeoJSON/GPX/shapefile/... included)
        for any deployment that hasn't configured Google Maps, even though none
        of those formats ever call out to Google. Explicitly force a blank key
        here rather than relying on this environment's own `.env` happening to
        leave it unset."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.google.maps.settings.google_unrestricted_api_key", ""
        ):
            lists = self._post("Urbex Sites.kml", _KML_TEMPLATE.encode())
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["stem"], "Urbex Sites")


class PreviewPinCapTests(TestCase):
    """The preview materialises every pin at once; the import does not.

    The confirmed import walks its pins one at a time. The preview that runs
    *first* builds every pin dict and serialises them into a single result, and
    nothing bounded that - which matters more since the archive extractor's
    budget became a shared 2 GB across an upload's nested archives.

    Uses a lowered cap rather than a 20,000-pin fixture: the property is that
    the bound is applied and reported, which a small cap demonstrates exactly
    as well and in a fraction of the time.
    """

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _geojson(self, count: int) -> bytes:
        features = [
            {
                "type": "Feature",
                "properties": {"name": f"Spot {index}"},
                "geometry": {"type": "Point", "coordinates": [-73.9 - index * 0.001, 41.7 + index * 0.001]},
            }
            for index in range(count)
        ]
        return json.dumps({"type": "FeatureCollection", "features": features}).encode()

    def _preview(self, data: bytes, cap: int) -> dict:
        upload = SimpleUploadedFile("spots.geojson", data, content_type="application/octet-stream")
        with mock.patch.object(GoogleMapsGateway, "MAX_PREVIEW_PINS", cap):
            return _read_preview(self, [upload])

    def test_the_preview_stops_at_the_cap(self) -> None:
        payload = self._preview(self._geojson(12), cap=5)

        self.assertEqual(payload["total"], 5)
        self.assertEqual(sum(len(lst["pins"]) for lst in payload["lists"]), 5)

    def test_hitting_the_cap_is_reported_rather_than_shown_silently(self) -> None:
        """A truncated preview otherwise looks exactly like a smaller file."""
        payload = self._preview(self._geojson(12), cap=5)

        self.assertTrue(any("preview limit" in warning for warning in payload["warnings"]), payload["warnings"])

    def test_an_upload_under_the_cap_is_untouched_and_unwarned(self) -> None:
        """Anti-vacuity: the bound must not alter or annotate an ordinary import."""
        payload = self._preview(self._geojson(3), cap=5)

        self.assertEqual(payload["total"], 3)
        self.assertFalse([w for w in payload["warnings"] if "preview limit" in w], payload["warnings"])
