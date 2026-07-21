"""Tests for PinController.parse_for_preview - the import wizard's file-parsing step.

Covers the suggested list/category name ("stem") derived from each uploaded
file - see docs/prompts/completed.md for the bug this guards against: a KMZ
is just a ZIP wrapping a single "doc.kml" (Google's own fixed internal
filename, from Google Takeout/My Maps exports), so using that inner name
unconditionally always produced the same generic "doc" suggestion regardless
of what the user actually named the .kmz.
"""

from __future__ import annotations

import io
import zipfile

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase

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


class ParseForPreviewStemTests(TestCase):
    """PinController.parse_for_preview - suggested list/category name per file."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)

    def _post(self, filename: str, data: bytes) -> list[dict]:
        upload = SimpleUploadedFile(filename, data, content_type="application/octet-stream")
        response = self.client.post(reverse("pin.import.preview"), {"upload_files": [upload]})
        self.assertEqual(response.status_code, 200)
        return response.json()["lists"]

    def test_kmz_wrapping_doc_kml_uses_the_outer_filename(self) -> None:
        """The exact reported bug: Google Takeout/My Maps KMZ always names its
        single internal file "doc.kml" - the suggested stem must come from
        the .kmz the user actually uploaded, not that generic inner name."""
        lists = self._post("My Saved Places.kmz", _kmz_bytes("doc.kml"))
        self.assertEqual(len(lists), 1)
        self.assertEqual(lists[0]["stem"], "My Saved Places")

    def test_plain_kml_upload_uses_its_own_filename(self) -> None:
        """Sanity baseline: a non-archive upload is completely unaffected."""
        upload = SimpleUploadedFile("Urbex Sites.kml", _KML_TEMPLATE.encode(), content_type="application/vnd.google-earth.kml+xml")
        response = self.client.post(reverse("pin.import.preview"), {"upload_files": [upload]})
        lists = response.json()["lists"]
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
