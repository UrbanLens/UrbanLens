"""The Private Pin map's Land Use layer: the Special Land Use Area boundaries REData draws around a pin's parcel."""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.core.cache import cache, caches
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_MANUAL_ONLY,
    REASON_SOURCE_ERROR,
    PropertyRecordsBusyError,
    PropertyRecordsUnavailableError,
)
from urbanlens.dashboard.services.integration_testing.accounts import prepare_signed_in_account
from urbanlens.dashboard.services.map import land_use_areas
from urbanlens.dashboard.templatetags.map_components import MAP_LAYER_REGISTRY

_GATEWAY = "urbanlens.dashboard.services.map.land_use_areas.RedataGateway"
_CONFIGURED = "urbanlens.dashboard.services.map.land_use_areas.redata_configured"

_FORT = {
    "name": "FORT LIBERTY",
    "geoid": "1234",
    "geometry": {"type": "Polygon", "coordinates": [[[-79.1, 35.1], [-78.9, 35.1], [-78.9, 35.2], [-79.1, 35.1]]]},
}
_CAMPUS = {
    "name": "STATE UNIVERSITY",
    "geoid": None,
    "geometry": {
        "type": "MultiPolygon",
        "coordinates": [[[[-79.0, 35.1], [-78.95, 35.1], [-78.95, 35.15], [-79.0, 35.1]]]],
    },
}


def _record(**fields: object) -> dict:
    return {"available": True, "uuid": "parcel-uuid", **fields}


class _LayerTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        caches[django_settings.PROXIED_BYTES_CACHE].clear()
        self.user = baker.make(User)
        self.profile = prepare_signed_in_account(self.user)
        self.location = baker.make(Location, latitude=35.14, longitude=-79.0, address="1 Example Rd")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        configured = mock.patch(_CONFIGURED, return_value=True)
        configured.start()
        self.addCleanup(configured.stop)
        gateway = mock.patch(_GATEWAY)
        self.gateway = gateway.start().return_value
        self.addCleanup(gateway.stop)

    def _cache_record(self, payload: dict) -> None:
        LocationCache.set(self.location, "property_records", payload)


class CollectionTests(_LayerTestCase):
    def test_draws_each_area_the_parcel_is_inside(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}, "college_university": {}}))
        self.gateway.lookup_land_use_areas.return_value = {
            "college_university": _CAMPUS,
            "military_installation": _FORT,
        }

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["type"], "FeatureCollection")
        self.assertEqual(answer["status"], "found")
        self.assertTrue(answer["complete"])
        self.gateway.lookup_land_use_areas.assert_called_once_with("parcel-uuid")
        features = answer["features"]
        self.assertEqual(
            [f["properties"]["category"] for f in features], ["college_university", "military_installation"]
        )
        self.assertEqual(features[1]["properties"]["name"], "FORT LIBERTY")
        self.assertEqual(features[1]["properties"]["label"], "Military installation")
        self.assertEqual(features[1]["geometry"], _FORT["geometry"])
        self.assertEqual(features[0]["geometry"]["type"], "MultiPolygon")

    def test_a_parcel_in_no_area_asks_redata_nothing_more(self) -> None:
        self._cache_record(_record(special_land_use_areas={}))

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual((answer["status"], answer["features"]), ("none", []))
        self.gateway.lookup_land_use_areas.assert_not_called()
        self.gateway.lookup_parcel.assert_not_called()

    def test_a_record_that_could_not_ask_every_source_is_not_trusted_to_say_none(self) -> None:
        self._cache_record(_record(special_land_use_areas={}, unanswered_sources=[{"tier": 2, "status": "error"}]))
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["status"], "found")

    def test_a_record_from_a_redata_that_sends_no_flags_still_asks(self) -> None:
        self._cache_record(_record())
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["status"], "found")
        self.assertTrue(answer["complete"])

    def test_a_second_toggle_is_answered_from_the_cache(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}}))
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        first = land_use_areas.land_use_area_collection(self.pin)
        second = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(first, second)
        self.gateway.lookup_land_use_areas.assert_called_once()

    def test_an_area_redata_flagged_but_did_not_draw_is_a_partial_answer_kept_briefly(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}, "national_park": {}}))
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        with mock.patch.object(land_use_areas, "set_or_skip", wraps=land_use_areas.set_or_skip) as stored:
            answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertFalse(answer["complete"])
        self.assertEqual(len(answer["features"]), 1)
        self.assertEqual(stored.call_args.args[2], land_use_areas.PARTIAL_ANSWER_SECONDS)

    def test_a_complete_answer_is_kept_for_the_long_window(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}}))
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        with mock.patch.object(land_use_areas, "set_or_skip", wraps=land_use_areas.set_or_skip) as stored:
            land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(stored.call_args.args[2], land_use_areas.COMPLETE_ANSWER_SECONDS)

    def test_an_area_without_usable_geometry_is_not_drawn(self) -> None:
        self._cache_record(_record())
        self.gateway.lookup_land_use_areas.return_value = {
            "military_installation": {**_FORT, "geometry": None},
            "national_park": {**_FORT, "geometry": {"type": "Point", "coordinates": [-79.0, 35.1]}},
            "correctional_facility": {**_FORT, "geometry": {"type": "Polygon", "coordinates": "nope"}},
        }

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["features"], [])
        self.assertEqual(answer["status"], "none")

    def test_an_unfamiliar_category_is_drawn_under_a_readable_label(self) -> None:
        self._cache_record(_record())
        self.gateway.lookup_land_use_areas.return_value = {"tribal_trust_land": _FORT}

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["features"][0]["properties"]["label"], "Tribal trust land")

    def test_without_a_cached_record_it_shares_the_property_panel_lookup(self) -> None:
        self.gateway.lookup_parcel.return_value = _record(special_land_use_areas={"military_installation": {}})
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["status"], "found")
        self.gateway.lookup_parcel.assert_called_once_with(35.14, -79.0, situs_address="1 Example Rd")

    def test_a_place_redata_has_no_parcel_for_says_so(self) -> None:
        self.gateway.lookup_parcel.side_effect = PropertyRecordsUnavailableError(
            REASON_MANUAL_ONLY, "Call the assessor."
        )

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual((answer["status"], answer["features"]), ("no_parcel", []))

    def test_a_cached_record_with_no_parcel_says_so(self) -> None:
        self._cache_record({"available": False, "reason": REASON_MANUAL_ONLY})

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["status"], "no_parcel")
        self.gateway.lookup_parcel.assert_not_called()

    def test_a_redata_without_the_endpoint_leaves_the_layer_empty(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}}))
        self.gateway.lookup_land_use_areas.side_effect = PropertyRecordsUnavailableError(
            REASON_SOURCE_ERROR, "", status_code=404
        )

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual((answer["status"], answer["features"]), ("unavailable", []))

    def test_an_outage_raises_and_is_not_asked_again_at_once(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}}))
        self.gateway.lookup_land_use_areas.side_effect = PropertyRecordsBusyError(
            "key_budget_exhausted", "spent", retry_after=900
        )

        with self.assertRaises(land_use_areas.LandUseAreasBusyError) as first:
            land_use_areas.land_use_area_collection(self.pin)
        with self.assertRaises(land_use_areas.LandUseAreasBusyError) as second:
            land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(first.exception.retry_after, 900)
        self.assertGreater(second.exception.retry_after, 0)
        self.gateway.lookup_land_use_areas.assert_called_once()

    def test_an_outage_of_the_parcel_lookup_raises(self) -> None:
        self.gateway.lookup_parcel.side_effect = PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "down")

        with self.assertRaises(land_use_areas.LandUseAreasBusyError):
            land_use_areas.land_use_area_collection(self.pin)

    def test_without_redata_the_layer_is_unavailable_and_nothing_is_asked(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["status"], "unavailable")
        self.gateway.lookup_parcel.assert_not_called()

    def test_outside_the_united_states_nothing_is_asked(self) -> None:
        self.location.latitude, self.location.longitude = 51.5, -0.12
        self.location.save()

        answer = land_use_areas.land_use_area_collection(self.pin)

        self.assertEqual(answer["status"], "outside_coverage")
        self.gateway.lookup_parcel.assert_not_called()


class LayerOfferedTests(_LayerTestCase):
    def test_offered_for_a_us_pin_when_redata_is_configured(self) -> None:
        self.assertTrue(land_use_areas.land_use_layer_offered(self.pin))

    def test_not_offered_without_redata(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            self.assertFalse(land_use_areas.land_use_layer_offered(self.pin))

    def test_not_offered_outside_the_united_states(self) -> None:
        self.location.latitude, self.location.longitude = 51.5, -0.12
        self.location.save()
        self.assertFalse(land_use_areas.land_use_layer_offered(self.pin))

    def test_the_layer_button_is_a_custom_toggle(self) -> None:
        layer = MAP_LAYER_REGISTRY[land_use_areas.LAND_USE_LAYER_KEY]
        self.assertEqual(layer.kind, "custom")
        self.assertEqual(layer.label, "Land Use")


class LandUseAreasViewTests(_LayerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("pin.land_use_areas.json", kwargs={"pin_slug": self.pin.ensure_slug()})

    def test_returns_the_collection_with_a_private_browser_cache(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}}))
        self.gateway.lookup_land_use_areas.return_value = {"military_installation": _FORT}

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        body = json.loads(response.content)
        self.assertEqual(body["status"], "found")
        self.assertEqual(len(body["features"]), 1)
        self.assertEqual(response["Cache-Control"], "private, max-age=300")

    def test_an_outage_is_a_503_naming_the_wait(self) -> None:
        self._cache_record(_record(special_land_use_areas={"military_installation": {}}))
        self.gateway.lookup_land_use_areas.side_effect = PropertyRecordsBusyError(
            "rate_limited", "busy", retry_after=120
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "120")
        self.assertIn("error", json.loads(response.content))

    def test_another_accounts_pin_is_not_found(self) -> None:
        other = baker.make(Pin, profile=prepare_signed_in_account(baker.make(User)), location=self.location)

        response = self.client.get(reverse("pin.land_use_areas.json", kwargs={"pin_slug": other.ensure_slug()}))

        self.assertEqual(response.status_code, 404)
        self.gateway.lookup_land_use_areas.assert_not_called()

    def test_the_pin_page_offers_the_layer_off_by_default(self) -> None:
        response = self.client.get(reverse("pin.details", kwargs={"pin_slug": self.pin.ensure_slug()}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-map-layer="landuse"')
        self.assertContains(response, f'data-land-use-areas-json-url="{self.url}"')

    def test_the_pin_page_leaves_the_layer_out_without_redata(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            response = self.client.get(reverse("pin.details", kwargs={"pin_slug": self.pin.ensure_slug()}))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-map-layer="landuse"')
