"""Catalogued map volumes (REData ``/maps/volumes/``) listed in the historical-map browse beside the covering sheets."""

from __future__ import annotations

from unittest import mock
import uuid as uuid_module

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.controllers.map_overlays import HISTORICAL_MAP_VOLUME_SHEETS, historical_volume_row
from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.services.apis import request_upstreams
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway import RedataHistoricalMapsGateway

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_historical_maps_gateway.RedataHistoricalMapsGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"

LATITUDE, LONGITUDE = 42.355, -71.055
COVERING = [-71.06, 42.35, -71.05, 42.36]
ABOUT_A_KM_AWAY = [-71.07, 42.36, -71.065, 42.365]
ACROSS_TOWN = [-71.2, 42.4, -71.19, 42.41]


def _georeference(bounds: list[float], *, georeference_uuid: str | None = None) -> dict:
    """A georeference in REData's ``MapGeoreferenceSerializer`` shape."""
    uuid = georeference_uuid or str(uuid_module.uuid4())
    return {
        "uuid": uuid,
        "source": "allmaps",
        "status": "ready",
        "is_preferred": True,
        "annotation_id": "https://annotations.allmaps.org/images/abc",
        "annotation_url": f"https://redata.example.test/api/v1/maps/georeferences/{uuid}/annotation/",
        "tile_url_template": f"https://redata.example.test/api/v1/maps/georeferences/{uuid}/tiles/{{z}}/{{x}}/{{y}}.png",
        "bounds": bounds,
        "is_drawable": True,
        "gcp_count": 8,
        "discarded_gcp_count": 0,
        "transformation": "polynomial",
        "transformation_order": 1,
        "has_explicit_mask": True,
        "area_sq_meters": 250000.0,
        "rmse_meters": 4.2,
        "attributes": {},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
    }


def _sheet(sequence: int, georeference: dict | None = None) -> dict:
    """A sheet in REData's ``VolumeSheetSerializer`` shape."""
    return {
        "uuid": str(uuid_module.uuid4()),
        "sequence": sequence,
        "label": f"Sheet {sequence}",
        "image_service_id": f"https://tile.loc.gov/image-services/iiif/sanborn-{sequence}",
        "image_api_version": "2",
        "iiif_info_url": f"https://tile.loc.gov/image-services/iiif/sanborn-{sequence}/info.json",
        "image_width": 6000,
        "image_height": 7000,
        "thumbnail_url": f"https://tile.loc.gov/thumb/sanborn-{sequence}.jpg",
        "landing_page_url": f"https://www.loc.gov/resource/sanborn03760_002/?sp={sequence}",
        "is_georeferenced": georeference is not None,
        "georeference": georeference,
    }


def _volume_match(*sheets: dict, status: str = "harvested", **volume: object) -> dict:
    """A row in REData's ``VolumeMatchSerializer`` shape."""
    return {
        "volume": {
            "uuid": str(uuid_module.uuid4()),
            "provider": "loc",
            "external_id": "sanborn03760_002",
            "kind": "fire_insurance",
            "title": "Sanborn Fire Insurance Map from Boston, Suffolk County, Massachusetts",
            "description": "",
            "date_text": "1897",
            "year_start": 1897,
            "year_end": 1897,
            "attribution": "Library of Congress",
            "license": "No known restrictions on publication.",
            "place_name": "Boston",
            "landing_page_url": "https://www.loc.gov/item/sanborn03760_002/",
            "thumbnail_url": "https://tile.loc.gov/thumb/sanborn-volume.jpg",
            "sheet_count": len(sheets),
            "sheets_status": status,
            "attributes": {},
            "record_retrieved_at": "2026-09-01T00:00:00Z",
            **volume,
        },
        "place": {"name": "Boston", "level": "locality", "resolver": "geonames"},
        "match": "near",
        "distance_meters": 0.0,
        "sheets": list(sheets),
    }


def _covering_match(georeference_uuid: str) -> dict:
    """A covering-sheet match in REData's ``MapMatchSerializer`` shape, as far as the browse reads it."""
    return {
        "sheet": {"title": "Boston, 1897, Sheet 3", "date_text": "1897", "kind": "fire_insurance"},
        "georeference": _georeference(COVERING, georeference_uuid=georeference_uuid),
        "contains_point": True,
        "distance_meters": 0.0,
    }


class VolumesGatewayTests(SimpleTestCase):
    def test_asks_the_volumes_path_and_keeps_rows_that_carry_a_volume(self) -> None:
        response = mock.Mock(status_code=200)
        response.json.return_value = {"count": 2, "results": [_volume_match(_sheet(1)), {"place": {}}]}
        session = mock.Mock()
        session.get.return_value = response
        gateway = RedataHistoricalMapsGateway(base_url="https://redata.example.test", api_key="k", session=session)

        rows = gateway.get_volumes_near(LATITUDE, LONGITUDE, radius_meters=2000)

        self.assertEqual(len(rows), 1)
        self.assertTrue(session.get.call_args.args[0].endswith("/api/v1/maps/volumes/"))
        self.assertEqual(
            session.get.call_args.kwargs["params"], {"lat": LATITUDE, "lng": LONGITUDE, "radius_meters": 2000}
        )


class VolumeRowTests(SimpleTestCase):
    def test_the_nearest_placed_sheets_not_already_listed_are_addable(self) -> None:
        listed_uuid = str(uuid_module.uuid4())
        match = _volume_match(
            _sheet(1, _georeference(ACROSS_TOWN)),
            _sheet(2),
            _sheet(3, _georeference(COVERING, georeference_uuid=listed_uuid)),
            _sheet(4, _georeference(ABOUT_A_KM_AWAY)),
        )

        row = historical_volume_row(match, LATITUDE, LONGITUDE, {listed_uuid})

        assert row is not None
        self.assertEqual([sheet["label"] for sheet in row["sheets"]], ["Sheet 4", "Sheet 1"])
        self.assertEqual((row["sheet_count"], row["placed_count"]), (4, 3))
        self.assertAlmostEqual(row["sheets"][0]["km"], 1.0, delta=0.2)
        self.assertFalse(row["sheets"][0]["contains_point"])
        self.assertEqual(row["place_name"], "Boston")

    def test_a_sheet_covering_the_spot_says_so(self) -> None:
        row = historical_volume_row(_volume_match(_sheet(1, _georeference(COVERING))), LATITUDE, LONGITUDE, set())
        assert row is not None
        self.assertTrue(row["sheets"][0]["contains_point"])

    def test_a_large_volume_offers_only_its_nearest_few(self) -> None:
        sheets = [_sheet(index, _georeference(ACROSS_TOWN)) for index in range(HISTORICAL_MAP_VOLUME_SHEETS + 3)]
        row = historical_volume_row(_volume_match(*sheets), LATITUDE, LONGITUDE, set())
        assert row is not None
        self.assertEqual(len(row["sheets"]), HISTORICAL_MAP_VOLUME_SHEETS)
        self.assertEqual(row["placed_count"], HISTORICAL_MAP_VOLUME_SHEETS + 3)

    def test_years_stand_in_for_a_missing_date(self) -> None:
        row = historical_volume_row(
            _volume_match(date_text="", year_start=1897, year_end=1902), LATITUDE, LONGITUDE, set()
        )
        assert row is not None
        self.assertEqual(row["date_text"], "1897-1902")


class _BrowseCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        for upstream in (
            request_upstreams.HistoricalMapsBrowseUpstream,
            request_upstreams.HistoricalMapVolumesUpstream,
        ):
            upstream.reset()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        location = baker.make("dashboard.Location", latitude=LATITUDE, longitude=LONGITUDE)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.user.profile, location=location)
        self.url = reverse("pin.overlays.historical", args=[self.pin.slug])
        self.listed_uuid = str(uuid_module.uuid4())
        self.nearby_uuid = str(uuid_module.uuid4())
        self.enterContext(mock.patch(_CONFIGURED_PATH, return_value=True))
        self.gateway = self.enterContext(mock.patch(_GATEWAY_PATH)).return_value
        self.gateway.get_maps_covering.return_value = [_covering_match(self.listed_uuid)]

    def volumes(self, *, status: str = "harvested") -> dict:
        match = _volume_match(
            _sheet(3, _georeference(COVERING, georeference_uuid=self.listed_uuid)),
            _sheet(12, _georeference(ABOUT_A_KM_AWAY, georeference_uuid=self.nearby_uuid)),
            _sheet(13),
            status=status,
        )
        self.gateway.get_volumes_near.return_value = [match]
        return match


class VolumesBrowseTests(_BrowseCase):
    def test_the_place_atlas_lists_under_the_covering_sheets(self) -> None:
        self.volumes()
        body = self.client.get(self.url).content.decode()

        self.assertIn("Catalogued atlases of this place", body)
        self.assertIn("Sanborn Fire Insurance Map from Boston", body)
        self.assertIn("3 sheets, 2 placed on the map", body)
        self.assertIn("https://www.loc.gov/item/sanborn03760_002/", body)
        self.assertEqual(body.count(self.listed_uuid), 1, "a sheet the covering list offers is not offered twice")
        self.assertIn(self.nearby_uuid, body)
        self.assertNotIn("tile.loc.gov", body, "the volume's thumbnail is shown from this site's copy")

    def test_a_volume_still_being_listed_says_so_and_is_asked_again(self) -> None:
        self.volumes(status="pending")
        self.gateway.get_volumes_near.return_value[0]["sheets"] = []
        first = self.client.get(self.url).content.decode()
        self.client.get(self.url)

        self.assertIn("still being listed", first)
        self.assertEqual(self.gateway.get_volumes_near.call_count, 2)
        self.assertEqual(self.gateway.get_maps_covering.call_count, 1)

    def test_a_settled_answer_is_cached(self) -> None:
        self.volumes()
        self.client.get(self.url)
        self.client.get(self.url)
        self.assertEqual(self.gateway.get_volumes_near.call_count, 1)

    def test_an_atlas_outage_leaves_the_covering_sheets(self) -> None:
        self.gateway.get_volumes_near.side_effect = LocationContextUnavailableError("source_error", "down")
        body = self.client.get(self.url).content.decode()

        self.assertIn(self.listed_uuid, body)
        self.assertIn("Catalogued atlases of this place are temporarily unavailable", body)

    def test_a_volumes_placed_sheet_is_added_with_its_canonical_name_and_bounds(self) -> None:
        self.volumes()
        self.client.post(self.url, data={"georeference_uuid": self.nearby_uuid})

        overlay = MapImageOverlay.objects.get(parent_pin=self.pin)
        self.assertEqual(
            overlay.name, "Sanborn Fire Insurance Map from Boston, Suffolk County, Massachusetts - Sheet 12 - 1897"
        )
        self.assertEqual(overlay.corners(), [[42.365, -71.07], [42.365, -71.065], [42.36, -71.065], [42.36, -71.07]])
        self.assertTrue(overlay.locked)
        self.assertIn(self.nearby_uuid, overlay.tile_url_template)

    def test_a_uuid_in_neither_list_adds_nothing(self) -> None:
        self.volumes()
        self.client.post(self.url, data={"georeference_uuid": str(uuid_module.uuid4())})
        self.assertFalse(MapImageOverlay.objects.filter(parent_pin=self.pin).exists())


class VolumesWithoutRedataTests(TestCase):
    def test_nothing_is_asked_without_redata(self) -> None:
        user = baker.make(User)
        self.client.force_login(user)
        pin = baker.make_recipe("dashboard.pin", profile=user.profile)
        with mock.patch(_CONFIGURED_PATH, return_value=False), mock.patch(_GATEWAY_PATH) as gateway_cls:
            body = self.client.get(reverse("pin.overlays.historical", args=[pin.slug])).content.decode()

        gateway_cls.assert_not_called()
        self.assertIn("available on this install", body)
        self.assertNotIn("Catalogued atlases", body)
