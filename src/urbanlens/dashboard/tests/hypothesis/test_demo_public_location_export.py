"""The public-location export/import pair, and the boundary it must not cross.

The export exists to move *public* places onto a demo instance. The costly
mistake would be treating "has a wiki" as public: wiki visibility is earned per
viewer, and exporting on that basis would publish every location any user has
pinned. These tests pin the definition.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinCandidateStatus
from urbanlens.dashboard.models.wiki.model import Wiki


def _export() -> dict:
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory) / "export.json"
        call_command("export_public_locations", out=str(out), indent=0)
        return json.loads(out.read_text(encoding="utf-8"))


class PublicLocationExportTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.700000", longitude="-73.900000", google_place=None, official_name="Passed Place")

    def test_a_passed_candidate_is_exported(self) -> None:
        baker.make(PublicPinCandidate, location=self.location, status=PublicPinCandidateStatus.PASSED)

        payload = _export()

        self.assertEqual(len(payload["locations"]), 1)
        self.assertEqual(payload["locations"][0]["official_name"], "Passed Place")

    def test_a_location_with_a_wiki_but_no_passed_vote_is_not_exported(self) -> None:
        """Having a wiki is not being public - wiki access is earned per viewer."""
        Wiki.objects.create(location=self.location, name="Private Wiki", officially_created=True)

        self.assertEqual(_export()["locations"], [])

    def test_an_open_candidate_is_not_exported(self) -> None:
        """A vote still in progress has not made anything public yet."""
        baker.make(PublicPinCandidate, location=self.location, status=PublicPinCandidateStatus.OPEN)

        self.assertEqual(_export()["locations"], [])

    def test_no_authored_wiki_content_travels_with_a_public_location(self) -> None:
        """Comments, articles and edits describe people, not places."""
        baker.make(PublicPinCandidate, location=self.location, status=PublicPinCandidateStatus.PASSED)
        Wiki.objects.create(location=self.location, name="Public Wiki", officially_created=True)

        entry = _export()["locations"][0]

        self.assertEqual(set(entry["wiki"].keys()), {"name", "aliases", "photos"})
