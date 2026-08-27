"""Export -> import -> seed, end to end.

The point of the round trip is the last step: a demo account must end up holding
a pin on a *real* imported location, because holding that pin is what earns it
access to the place's wiki. Coordinates are never invented - a pin at an
unsurveyed point resolves no boundary, no parcel and no wiki, which reads as the
product being broken.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.public_pins.model import PublicPinCandidate, PublicPinCandidateStatus
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.demo.seeding import seed_demo_account
from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids


class DemoLocationRoundTripTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / "manifest.json"
        self.export = Path(self.directory.name) / "export.json"

    def _publish_a_location(self) -> Location:
        location = baker.make(Location, latitude="41.700000", longitude="-73.900000", google_place=None, official_name="Passed Place")
        baker.make(PublicPinCandidate, location=location, status=PublicPinCandidateStatus.PASSED)
        Wiki.objects.create(location=location, name="Passed Place")
        return location

    def _run_round_trip(self) -> None:
        call_command("export_public_locations", out=str(self.export), indent=0)
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest)):
            call_command("import_public_locations", str(self.export), allow_non_demo=True)

    def test_a_seeded_account_is_pinned_to_the_imported_location(self) -> None:
        source = self._publish_a_location()
        self._run_round_trip()

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest)):
            user = seed_demo_account()

        pinned = Pin.objects.filter(profile=user.profile).values_list("location__latitude", "location__longitude")
        self.assertEqual(
            {(str(lat), str(lng)) for lat, lng in pinned},
            {(str(source.latitude), str(source.longitude))},
        )

    def test_that_pin_is_what_grants_the_wiki(self) -> None:
        """Wiki visibility is earned by holding a pin on the location."""
        self._publish_a_location()
        self._run_round_trip()

        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest)):
            user = seed_demo_account()

        visible = visible_wiki_location_ids(user.profile)
        self.assertEqual(len(visible), 1, "the demo account should see exactly the wiki it was pinned to")

    def test_the_manifest_only_names_locations_that_were_imported(self) -> None:
        """A manifest entry with no Location behind it seeds an empty detail page."""
        self._publish_a_location()
        self._run_round_trip()

        entries = json.loads(self.manifest.read_text(encoding="utf-8"))["locations"]
        for entry in entries:
            self.assertTrue(
                Location.objects.filter(latitude=entry["latitude"], longitude=entry["longitude"]).exists(),
                f"manifest names {entry} but no Location exists for it",
            )
