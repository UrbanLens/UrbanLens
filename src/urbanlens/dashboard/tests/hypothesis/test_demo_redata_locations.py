"""REData's /public-locations/ catalog as a demo location source, and the merged manifest.

REData's endpoint is real (documented, tested code on its own repo) but not yet
deployed anywhere UrbanLens can reach - every test here pins the "degrades to
empty, never raises" contract that fact requires, plus the merge semantics that
let two independent importers (this site's own public pins, REData's catalog)
both write into one manifest safely.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_public_locations_gateway import RedataPublicLocationsGateway
from urbanlens.dashboard.services.demo.locations import merge_into_manifest, read_manifest, redata_demo_locations

# Both imported *inside* redata_demo_locations() (module-level import would be
# a demo-only dependency at import time for every process), so patching them
# has to target where they are actually defined, not locations.py's namespace.
_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_public_locations_gateway.RedataPublicLocationsGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

_RECORD = {
    "uuid": "11111111-1111-1111-1111-111111111111",
    "kind": "state_capitol",
    "name": "Example City",
    "country": "US",
    "state": "CT",
    "county_name": "",
    "latitude": 41.7,
    "longitude": -72.7,
    "source": "overpass",
    "catalog_synced_at": "2026-08-01T00:00:00Z",
}


class RedataPublicLocationsGatewayTests(SimpleTestCase):
    def test_a_non_200_response_degrades_to_an_empty_list_rather_than_raising(self) -> None:
        """REData not having this endpoint deployed yet must not crash seeding."""
        gateway = RedataPublicLocationsGateway(base_url="https://redata.test", api_key="k")
        with mock.patch.object(RedataPublicLocationsGateway, "get_json", side_effect=LocationContextUnavailableError("not_found", "404")):
            self.assertEqual(gateway.list_public_locations(), [])

    def test_a_non_dict_or_missing_results_key_is_treated_as_empty(self) -> None:
        gateway = RedataPublicLocationsGateway(base_url="https://redata.test", api_key="k")
        with mock.patch.object(RedataPublicLocationsGateway, "get_json", return_value={"count": 0}):
            self.assertEqual(gateway.list_public_locations(), [])
        with mock.patch.object(RedataPublicLocationsGateway, "get_json", return_value=["not", "a", "dict"]):
            self.assertEqual(gateway.list_public_locations(), [])

    def test_results_pass_through_unchanged(self) -> None:
        gateway = RedataPublicLocationsGateway(base_url="https://redata.test", api_key="k")
        with mock.patch.object(RedataPublicLocationsGateway, "get_json", return_value={"count": 1, "results": [_RECORD]}):
            self.assertEqual(gateway.list_public_locations(), [_RECORD])

    def test_no_coordinate_is_sent_when_browsing_the_whole_catalog(self) -> None:
        """The whole point of using get_json over near_point: no lat/lng required."""
        gateway = RedataPublicLocationsGateway(base_url="https://redata.test", api_key="k")
        with mock.patch.object(RedataPublicLocationsGateway, "get_json", return_value={"results": []}) as get_json:
            gateway.list_public_locations(kind="state_capitol")
        params = get_json.call_args.args[1]
        self.assertNotIn("lat", params)
        self.assertNotIn("lng", params)
        self.assertEqual(params["kind"], "state_capitol")


class RedataDemoLocationsTests(SimpleTestCase):
    def test_unconfigured_redata_yields_nothing(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=False):
            self.assertEqual(redata_demo_locations(), [])

    def test_a_configured_but_unreachable_redata_yields_nothing_not_an_exception(self) -> None:
        """A failure is caught inside list_public_locations itself (see the
        gateway tests above), so by the time redata_demo_locations calls it, an
        unreachable REData has already become an empty list, not something to
        catch again here - this just confirms that empty list passes through."""
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.list_public_locations.return_value = []
            self.assertEqual(redata_demo_locations(), [])

    def test_records_missing_coordinates_are_dropped(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.list_public_locations.return_value = [_RECORD, {**_RECORD, "latitude": None}]
            entries = redata_demo_locations()
        self.assertEqual(len(entries), 1)

    def test_entries_are_export_shaped_with_a_wiki_stub(self) -> None:
        with mock.patch(_CONFIGURED_PATH, return_value=True), mock.patch(_GATEWAY_PATH) as gateway_cls:
            gateway_cls.return_value.list_public_locations.return_value = [_RECORD]
            entries = redata_demo_locations()
        self.assertEqual(entries, [{"latitude": 41.7, "longitude": -72.7, "official_name": "Example City", "wiki": {"name": "Example City", "aliases": [], "photos": []}}])


class ManifestMergeTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manifest = Path(self.directory.name) / "manifest.json"

    def _entries(self, *pairs: tuple[float, float]) -> list[dict]:
        return [{"latitude": lat, "longitude": lng, "official_name": f"{lat},{lng}", "wiki": None} for lat, lng in pairs]

    def test_merging_into_an_empty_manifest_writes_everything(self) -> None:
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest)):
            merge_into_manifest(self._entries((1.0, 2.0), (3.0, 4.0)))
            self.assertEqual(len(read_manifest()), 2)

    def test_a_second_import_does_not_duplicate_a_coordinate_already_present(self) -> None:
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest)):
            merge_into_manifest(self._entries((1.0, 2.0)))
            merge_into_manifest(self._entries((1.0, 2.0), (5.0, 6.0)))
            entries = read_manifest()
        self.assertEqual(len(entries), 2)
        self.assertEqual({(e["latitude"], e["longitude"]) for e in entries}, {(1.0, 2.0), (5.0, 6.0)})

    def test_two_independent_importers_both_contribute(self) -> None:
        """import_public_locations then import_redata_public_locations, or the reverse - neither erases the other."""
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest)):
            merge_into_manifest(self._entries((1.0, 2.0)))  # e.g. this site's own public pins
            merge_into_manifest(self._entries((9.0, 9.0)))  # e.g. REData's catalog
            entries = read_manifest()
        self.assertEqual({(e["latitude"], e["longitude"]) for e in entries}, {(1.0, 2.0), (9.0, 9.0)})

    def test_no_manifest_path_configured_is_a_silent_noop(self) -> None:
        with mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", ""):
            self.assertIsNone(merge_into_manifest(self._entries((1.0, 2.0))))
