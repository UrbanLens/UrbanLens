"""A park's places and webcams on the NPS card, and each park facet asked once per park rather than once per pin."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.plugins.builtin.nps import NpsPanelSource, nearest_places, park_facts, webcam_rows
from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway import RedataNationalParksGateway

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_national_parks_gateway.RedataNationalParksGateway"
_CONFIGURED_PATH = "urbanlens.dashboard.plugins.builtin.nps.redata_configured"

PIN_LATITUDE, PIN_LONGITUDE = 44.46, -110.83


def _place(name: str, latitude: float | None, longitude: float | None, category: str = "Historic Building") -> dict:
    """A row in REData's ``PointOfInterestSerializer`` shape."""
    return {
        "uuid": "6f1e2d3c-0000-4000-8000-000000000001",
        "provider": "nps",
        "provenance": "",
        "external_id": f"place-{name}",
        "name": name,
        "category": category,
        "description": "",
        "url": f"https://www.nps.gov/places/{name.lower().replace(' ', '-')}.htm",
        "latitude": latitude,
        "longitude": longitude,
        "attributes": {},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
        "created": "2026-09-01T00:00:00Z",
        "updated": "2026-09-01T00:00:00Z",
    }


def _webcam(title: str, url: str, *, is_live: bool = False) -> dict:
    """A row in REData's ``MediaItemSerializer`` shape, as ``/parks/{code}/webcams/`` serves it."""
    return {
        "uuid": "6f1e2d3c-0000-4000-8000-000000000002",
        "provider": "nps",
        "external_id": f"webcam-{title}",
        "kind": "webcam",
        "title": title,
        "description": "",
        "url": url,
        "thumbnail_url": "",
        "cached_url": "",
        "credit": "NPS",
        "is_live": is_live,
        "duration_seconds": None,
        "embed_url": "https://www.nps.gov/webcams-yell/oldfaithful.jpg",
        "embed_kind": "image",
        "embed_refresh_seconds": 60,
        "embed_checked_at": None,
        "latitude": None,
        "longitude": None,
        "attributes": {},
        "record_retrieved_at": "2026-09-01T00:00:00Z",
        "is_aerial": False,
        "confidence": None,
        "created": "2026-09-01T00:00:00Z",
        "updated": "2026-09-01T00:00:00Z",
    }


PLACES = [
    _place("Lake Hotel", 44.55, -110.40),
    _place("Old Faithful Inn", 44.4598, -110.8312),
    _place("Unplaced Exhibit", None, None),
    _place("", 44.46, -110.83),
]


class ParkFacetGatewayTests(SimpleTestCase):
    def _gateway(self, body: object) -> RedataNationalParksGateway:
        response = mock.Mock(status_code=200, text="")
        response.json.return_value = body
        session = mock.Mock()
        session.get.return_value = response
        return RedataNationalParksGateway(base_url="https://redata.example.test", api_key="k", session=session)

    def test_places_and_webcams_paths(self) -> None:
        gateway = self._gateway([_place("Old Faithful Inn", 44.4598, -110.8312)])
        self.assertEqual(gateway.get_places("yell")[0]["name"], "Old Faithful Inn")
        self.assertEqual(gateway.session.get.call_args.args[0], "https://redata.example.test/api/v1/parks/yell/places/")
        gateway.get_webcams("yell")
        self.assertEqual(
            gateway.session.get.call_args.args[0], "https://redata.example.test/api/v1/parks/yell/webcams/"
        )

    def test_every_pin_near_a_park_shares_one_ask_per_facet(self) -> None:
        gateway = self._gateway([{"id": 1, "title": "Road closed"}])
        for _ in range(3):
            gateway.get_alerts("yell")
            gateway.get_places("yell")
        self.assertEqual(gateway.session.get.call_count, 2)


class NearestPlacesTests(SimpleTestCase):
    def test_nearest_first_without_unplaceable_rows(self) -> None:
        rows = nearest_places(PLACES, PIN_LATITUDE, PIN_LONGITUDE)
        self.assertEqual([row["name"] for row in rows], ["Old Faithful Inn", "Lake Hotel"])
        self.assertLess(rows[0]["distance_meters"], 100)
        self.assertEqual(set(rows[0]), {"name", "category", "url", "distance_meters"})

    def test_an_unexpected_shape_is_nothing(self) -> None:
        self.assertEqual(nearest_places(None, PIN_LATITUDE, PIN_LONGITUDE), [])


class WebcamRowsTests(SimpleTestCase):
    def test_links_to_the_viewer_page_and_drops_rows_without_one(self) -> None:
        rows = webcam_rows(
            [_webcam("Old Faithful", "https://www.nps.gov/yell/webcam.htm", is_live=True), _webcam("x", "")]
        )
        self.assertEqual(
            rows, [{"title": "Old Faithful", "url": "https://www.nps.gov/yell/webcam.htm", "is_live": True}]
        )


class ParkFactsTests(SimpleTestCase):
    DATA = {
        "places": [
            {"name": "Old Faithful Inn", "category": "Hotel", "url": "", "distance_meters": 50},
            {"name": "Lake Hotel", "category": "Hotel", "url": "", "distance_meters": 41000},
        ],
        "webcams": [{"title": "Old Faithful", "url": "https://www.nps.gov/yell/webcam.htm", "is_live": True}],
    }

    def test_places_and_webcams_are_listed_in_the_viewers_units(self) -> None:
        rows = park_facts(self.DATA, show_facility_facets=True, units="mi")
        by_label = {row["label"]: row for row in rows}
        self.assertEqual(by_label["Nearest Places"]["value"], "Old Faithful Inn (0.0 mi), Lake Hotel (25.5 mi)")
        self.assertEqual(
            by_label["Webcam"],
            {"label": "Webcam", "value": "Old Faithful (live)", "href": "https://www.nps.gov/yell/webcam.htm"},
        )

    def test_hidden_with_the_other_facility_facets(self) -> None:
        labels = {row["label"] for row in park_facts(self.DATA, show_facility_facets=False)}
        self.assertFalse(labels & {"Nearest Places", "Webcam"})


class ParkFacetFetchTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.location = baker.make("dashboard.Location", latitude=PIN_LATITUDE, longitude=PIN_LONGITUDE)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)
        self.gateway_cls = self.enterContext(mock.patch(_GATEWAY_PATH))
        gateway = self.gateway_cls.return_value
        gateway.find_nearest_park.return_value = {"park_code": "yell", "full_name": "Yellowstone National Park"}
        gateway.get_alerts.return_value = []
        gateway.get_visitor_centers.return_value = []
        gateway.get_campgrounds.return_value = []
        gateway.get_places.return_value = PLACES
        gateway.get_webcams.return_value = [_webcam("Old Faithful", "https://www.nps.gov/yell/webcam.htm")]

    def _cached(self) -> dict:
        row = LocationCache.get_fresh(self.location, "nps")
        assert row is not None
        return row.data

    def test_the_nearest_places_and_webcams_are_cached_with_the_park(self) -> None:
        NpsPanelSource().fetch(self.pin)
        cached = self._cached()
        self.assertEqual([place["name"] for place in cached["places"]], ["Old Faithful Inn", "Lake Hotel"])
        self.assertEqual(cached["webcams"][0]["title"], "Old Faithful")

    def test_a_facet_redata_cannot_answer_leaves_the_card(self) -> None:
        self.gateway_cls.return_value.get_places.side_effect = LocationContextUnavailableError("source_error", "down")
        NpsPanelSource().fetch(self.pin)
        cached = self._cached()
        self.assertEqual(cached["places"], [])
        self.assertEqual(cached["full_name"], "Yellowstone National Park")

    def test_the_web_card_links_the_webcam_for_a_pin_inside_the_park(self) -> None:
        LocationCache.set(
            self.location, PropertyRecordsPanelSource.cache_source, {"containing_park": {"park_code": "yell"}}
        )
        with mock.patch(_CONFIGURED_PATH, return_value=True):
            NpsPanelSource().fetch(self.pin)
            self.client.force_login(self.pin.profile.user)
            body = self.client.get(reverse("pin.nps", args=[self.pin.slug])).content.decode()

        self.assertIn('href="https://www.nps.gov/yell/webcam.htm"', body)
        self.assertIn("Old Faithful Inn", body)
