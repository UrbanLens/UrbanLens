"""Tests for the EPA ECHO plugin's exact-site/nearby-list split.

Covers:
- EpaEchoDetailPanelSource shows an unconditional card when a facility's
  REData-reported coordinates are close enough to the pin's own to plausibly
  BE that pin, and 204s (renders nothing) otherwise.
- EpaEchoNearbyPanelSource lists nearby facilities, excluding whichever one
  was matched as the exact site (it already has its own card).
- EpaFacilityNameProvider only suggests a name when an exact-site match exists.
- _fetch_epa_echo_data's distance-based exact-match logic against a handful
  of REData points-of-interest rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.epa_echo import (
    EpaEchoDetailPanelSource,
    EpaEchoNearbyPanelSource,
    EpaFacilityNameProvider,
    _facility_from_poi,
    _fetch_epa_echo_data,
    _miles_between,
    _propagate_exact_site_to_nearby_locations,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

_GATEWAY_PATH = "urbanlens.dashboard.services.apis.locations.redata_points_of_interest_gateway.RedataPointsOfInterestGateway"


class MilesBetweenTests(SimpleTestCase):
    def test_same_point_is_zero_distance(self) -> None:
        self.assertAlmostEqual(_miles_between(40.0, -74.0, 40.0, -74.0), 0.0, places=6)

    def test_known_distance_is_approximately_correct(self) -> None:
        # ~1 degree of longitude at the equator is about 69 miles.
        self.assertAlmostEqual(_miles_between(0.0, 0.0, 0.0, 1.0), 69.17, delta=0.5)


class EpaEchoDetailPanelSourceTests(TestCase):
    """render_context() for the unconditional exact-site card."""

    def setUp(self) -> None:
        super().setUp()
        self.source = EpaEchoDetailPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_no_exact_site_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"facilities": [], "exact_site": None}))

    def test_empty_data_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {}))

    def test_exact_site_renders_heading_name(self) -> None:
        data = {"exact_site": {"name": "Old Mill Factory", "address": "123 Main St", "registry_id": "R1", "compliance_status": "In compliance"}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Old Mill Factory")

    def test_footer_link_uses_the_detailed_facility_report_url(self) -> None:
        data = {"exact_site": {"name": "Old Mill Factory", "address": "123 Main St", "registry_id": "R123", "compliance_status": "In compliance"}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["footer_link"]["url"], "https://echo.epa.gov/detailed-facility-report?fid=R123")

    def test_significant_noncompliance_surfaces_as_a_chip_and_meta_entry(self) -> None:
        data = {
            "exact_site": {
                "name": "Old Mill Factory",
                "address": "123 Main St",
                "registry_id": "R1",
                "compliance_status": "Significant Violator",
                "significant_violator": True,
                "quarters_in_noncompliance": "2",
                "last_inspection": "2025-01-01",
            },
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("Significant noncompliance", ctx["chips"])
        self.assertTrue(any(entry["label"] == "Significant noncompliance" for entry in ctx["meta"]))

    def test_clean_compliance_history_has_no_danger_chip(self) -> None:
        data = {
            "exact_site": {
                "name": "Old Mill Factory",
                "address": "123 Main St",
                "registry_id": "R1",
                "compliance_status": "In compliance",
                "significant_violator": False,
                "quarters_in_noncompliance": "0",
                "last_inspection": "2025-01-01",
            },
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], [])

    def test_missing_registry_id_falls_back_to_generic_echo_link(self) -> None:
        data = {"exact_site": {"name": "Old Mill Factory", "address": "123 Main St", "registry_id": ""}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["footer_link"]["url"], "https://echo.epa.gov/")


class EpaEchoDetailPanelSourceFetchLinkTests(TestCase):
    """fetch() must add the matched facility's EPA compliance report to the pin's
    (and wiki's) links - the same URL already shown inline via render_context's
    footer_link, but persisted as a real PinLink/WikiLink so it survives on the
    pin's own Links list, mirroring NominatimPanelSource._add_osm_link."""

    def setUp(self) -> None:
        super().setUp()
        self.source = EpaEchoDetailPanelSource()
        self.location: Location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=self.location)

    def _fetch_with(self, exact_site):
        from urbanlens.dashboard.models.links.model import PinLink

        with mock.patch(
            "urbanlens.dashboard.plugins.builtin.epa_echo._fetch_epa_echo_data",
            return_value={"facilities": [], "exact_site": exact_site},
        ):
            self.source.fetch(self.pin)
        return PinLink.objects.filter(pin=self.pin)

    def test_exact_site_match_adds_a_pin_link(self) -> None:
        links = self._fetch_with({"name": "Old Mill Factory", "address": "1 Main St", "registry_id": "R123"})
        self.assertTrue(links.filter(url="https://echo.epa.gov/detailed-facility-report?fid=R123").exists())

    def test_no_exact_site_adds_no_link(self) -> None:
        links = self._fetch_with(None)
        self.assertFalse(links.exists())

    def test_missing_registry_id_adds_no_link(self) -> None:
        links = self._fetch_with({"name": "Old Mill Factory", "address": "1 Main St", "registry_id": ""})
        self.assertFalse(links.exists())

    def test_link_is_also_added_to_the_locations_wiki_when_one_exists(self) -> None:
        from urbanlens.dashboard.models.links.model import WikiLink

        wiki: Wiki = baker.make("dashboard.Wiki", location=self.location)
        self._fetch_with({"name": "Old Mill Factory", "address": "1 Main St", "registry_id": "R123"})
        self.assertTrue(WikiLink.objects.filter(wiki=wiki, url="https://echo.epa.gov/detailed-facility-report?fid=R123").exists())

    def test_fetching_twice_does_not_duplicate_the_link(self) -> None:
        from urbanlens.dashboard.models.links.model import PinLink

        exact_site = {"name": "Old Mill Factory", "address": "1 Main St", "registry_id": "R123"}
        self._fetch_with(exact_site)
        self._fetch_with(exact_site)
        self.assertEqual(PinLink.objects.filter(pin=self.pin, url="https://echo.epa.gov/detailed-facility-report?fid=R123").count(), 1)


class EpaEchoNearbyPanelSourceTests(TestCase):
    """render_context() for the nearby-facility list, excluding the exact-site match."""

    def setUp(self) -> None:
        super().setUp()
        self.source = EpaEchoNearbyPanelSource()
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile)

    def test_no_facilities_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"facilities": [], "exact_site": None}))

    def test_lists_facility_names(self) -> None:
        data = {
            "facilities": [{"name": "Facility A", "address": "1 A St", "registry_id": "RA", "compliance_status": "In compliance"}],
            "exact_site": None,
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertTrue(any(entry["label"] == "Facility A" for entry in ctx["meta"]))

    def test_exact_site_match_is_excluded_from_the_nearby_list(self) -> None:
        data = {
            "facilities": [
                {"name": "Exact Match Facility", "address": "1 A St", "registry_id": "RA", "compliance_status": "In compliance"},
                {"name": "Other Facility", "address": "2 B St", "registry_id": "RB", "compliance_status": "In compliance"},
            ],
            "exact_site": {"registry_id": "RA", "name": "Exact Match Facility", "address": "1 A St"},
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = [entry["label"] for entry in ctx["meta"]]
        self.assertNotIn("Exact Match Facility", labels)
        self.assertIn("Other Facility", labels)

    def test_only_facility_being_the_exact_site_yields_none(self) -> None:
        """If the only nearby facility IS the exact site, there's nothing left for this list to show."""
        data = {
            "facilities": [{"name": "Exact Match Facility", "address": "1 A St", "registry_id": "RA", "compliance_status": "In compliance"}],
            "exact_site": {"registry_id": "RA", "name": "Exact Match Facility", "address": "1 A St"},
        }
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_each_facility_links_to_its_own_compliance_report(self) -> None:
        """Regression guard: this list used to have one generic footer_link to EPA
        ECHO's homepage instead of linking each entry to its own report."""
        data = {
            "facilities": [{"name": "Facility A", "address": "1 A St", "registry_id": "RA", "compliance_status": "In compliance"}],
            "exact_site": None,
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["href"], "https://echo.epa.gov/detailed-facility-report?fid=RA")
        self.assertNotIn("footer_link", ctx)

    def test_facility_with_no_registry_id_has_no_href(self) -> None:
        data = {
            "facilities": [{"name": "Facility A", "address": "1 A St", "registry_id": "", "compliance_status": "In compliance"}],
            "exact_site": None,
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["href"], "")


class EpaFacilityNameProviderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.provider = EpaFacilityNameProvider()
        self.location: Location = baker.make("dashboard.Location")

    def test_no_cache_row_yields_no_candidates(self) -> None:
        self.assertEqual(self.provider.candidates(self.location), [])

    def test_no_exact_site_yields_no_candidates(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(self.location, "epa_echo", {"facilities": [], "exact_site": None}, query_key="")
        self.assertEqual(self.provider.candidates(self.location), [])

    def test_exact_site_name_is_a_candidate(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(
            self.location,
            "epa_echo",
            {"facilities": [], "exact_site": {"name": "Old Mill Factory", "registry_id": "R1"}},
            query_key="",
        )
        self.assertEqual(self.provider.candidates(self.location), ["Old Mill Factory"])

    def test_never_suggests_a_merely_nearby_facility_name(self) -> None:
        """Regression guard: only the matched exact_site name is ever a candidate, never
        facilities[0] or any other nearby-list entry."""
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(
            self.location,
            "epa_echo",
            {"facilities": [{"name": "Some Nearby Factory", "registry_id": "R9"}], "exact_site": None},
            query_key="",
        )
        self.assertEqual(self.provider.candidates(self.location), [])


class FetchEpaEchoDataExactMatchTests(TestCase):
    """_fetch_epa_echo_data's distance-based exact-match selection, against a mocked
    RedataPointsOfInterestGateway. Unlike the direct EPA ECHO API this replaced, REData
    resolves every candidate's coordinates and compliance attributes in a single call -
    there is no separate, rate-limited per-candidate detail fetch left to test."""

    def setUp(self) -> None:
        super().setUp()
        self.pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=40.0, longitude=-74.0),
        )

    def _poi(self, *, registry_id: str, name: str, latitude: float | None, longitude: float | None, **attributes) -> dict:
        return {"external_id": registry_id, "name": name, "latitude": latitude, "longitude": longitude, "attributes": attributes}

    def test_no_facilities_returns_no_exact_site(self) -> None:
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = []
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_facility_at_pin_coordinates_is_the_exact_site(self) -> None:
        pois = [self._poi(registry_id="R1", name="Right Here Facility", latitude=40.0, longitude=-74.0)]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = pois
            result = _fetch_epa_echo_data(self.pin)
        assert result["exact_site"] is not None
        self.assertEqual(result["exact_site"]["registry_id"], "R1")
        self.assertEqual(result["exact_site"]["name"], "Right Here Facility")

    def test_facility_far_from_pin_coordinates_is_not_the_exact_site(self) -> None:
        pois = [self._poi(registry_id="R2", name="Far Away Facility", latitude=41.0, longitude=-75.0)]  # >0.1mi away
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = pois
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_closest_of_several_candidates_wins(self) -> None:
        pois = [
            self._poi(registry_id="R1", name="Slightly Off Facility", latitude=40.0005, longitude=-74.0),
            self._poi(registry_id="R2", name="Dead On Facility", latitude=40.0, longitude=-74.0),
        ]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = pois
            result = _fetch_epa_echo_data(self.pin)
        assert result["exact_site"] is not None
        self.assertEqual(result["exact_site"]["registry_id"], "R2")

    def test_facility_with_no_registry_id_is_skipped(self) -> None:
        pois = [self._poi(registry_id="", name="No Registry Facility", latitude=40.0, longitude=-74.0)]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = pois
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_facility_with_no_coordinates_is_skipped(self) -> None:
        pois = [self._poi(registry_id="R1", name="No Coords Facility", latitude=None, longitude=None)]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = pois
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_non_usa_coordinates_short_circuit_without_calling_the_gateway(self) -> None:
        pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=48.8566, longitude=2.3522),  # Paris
        )
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            result = _fetch_epa_echo_data(pin)
        self.assertEqual(result, {"facilities": [], "exact_site": None})
        mock_gateway_cls.return_value.find_near.assert_not_called()

    def test_facilities_are_persisted_to_epa_facility(self) -> None:
        from urbanlens.dashboard.models.epa_facility.model import EpaFacility

        pois = [self._poi(registry_id="R1", name="Persisted Facility", latitude=40.0, longitude=-74.0, compliance_status="In compliance")]
        with mock.patch(_GATEWAY_PATH) as mock_gateway_cls:
            mock_gateway_cls.return_value.find_near.return_value = pois
            _fetch_epa_echo_data(self.pin)

        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.name, "Persisted Facility")
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.data["compliance_status"], "In compliance")


class PropagateExactSiteToNearbyLocationsTests(TestCase):
    """_propagate_exact_site_to_nearby_locations: once an exact-site EPA match is
    confirmed for one Location, nearby pinned Locations whose own epa_echo cache
    has no match yet should immediately pick up the same match, instead of
    waiting on their own next fetch cycle (which could be stale for
    `SiteSettings.external_data_cache_days`)."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = baker.make(User).profile
        self.location: Location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.exact_site = {"name": "Old Mill Factory", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0, "longitude": -74.0}

    def _pinned_location(self, latitude: float, longitude: float) -> Location:
        from urbanlens.dashboard.models.pin.model import Pin

        location: Location = baker.make("dashboard.Location", latitude=latitude, longitude=longitude)
        baker.make(Pin, profile=self.owner, location=location)
        return location

    def _cached_data(self, location: Location) -> dict | None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        row = LocationCache.objects.filter(location=location, source="epa_echo").first()
        return row.data if row else None

    def test_creates_a_cache_row_for_a_nearby_pinned_location_with_no_row_yet(self) -> None:
        neighbor = self._pinned_location(40.0005, -74.0)  # ~0.03mi away

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        self.assertEqual(self._cached_data(neighbor), {"facilities": [], "exact_site": self.exact_site})

    def test_fills_in_a_nearby_locations_empty_exact_site(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        neighbor = self._pinned_location(40.0005, -74.0)
        LocationCache.set(neighbor, "epa_echo", {"facilities": [], "exact_site": None}, query_key="40.00050,-74.00000")

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        data = self._cached_data(neighbor)
        assert data is not None
        self.assertEqual(data["exact_site"], self.exact_site)

    def test_preserves_the_neighbors_existing_facilities_list(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        neighbor = self._pinned_location(40.0005, -74.0)
        nearby_list = [{"name": "Some Other Facility", "registry_id": "R9"}]
        LocationCache.set(neighbor, "epa_echo", {"facilities": nearby_list, "exact_site": None}, query_key="")

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        data = self._cached_data(neighbor)
        assert data is not None
        self.assertEqual(data["facilities"], nearby_list)
        self.assertEqual(data["exact_site"], self.exact_site)

    def test_does_not_overwrite_a_neighbor_with_its_own_confirmed_exact_site(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        neighbor = self._pinned_location(40.0005, -74.0)
        own_match = {"name": "Different Facility", "registry_id": "R2"}
        LocationCache.set(neighbor, "epa_echo", {"facilities": [], "exact_site": own_match}, query_key="")

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        data = self._cached_data(neighbor)
        assert data is not None
        self.assertEqual(data["exact_site"], own_match)

    def test_ignores_a_pinned_location_outside_the_exact_match_radius(self) -> None:
        far_neighbor = self._pinned_location(40.01, -74.0)  # ~0.69mi away

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        self.assertIsNone(self._cached_data(far_neighbor))

    def test_ignores_a_nearby_location_with_no_pins(self) -> None:
        unpinned: Location = baker.make("dashboard.Location", latitude=40.0005, longitude=-74.0)

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        self.assertIsNone(self._cached_data(unpinned))

    def test_missing_exact_site_coordinates_is_a_noop(self) -> None:
        neighbor = self._pinned_location(40.0005, -74.0)
        exact_site_no_coords = {"name": "Old Mill Factory", "registry_id": "R1"}

        _propagate_exact_site_to_nearby_locations(self.location, exact_site_no_coords)

        self.assertIsNone(self._cached_data(neighbor))

    def test_the_originating_location_itself_is_excluded(self) -> None:
        """The neighbor query must not try to re-propagate onto the same Location
        the match was just confirmed for."""
        from urbanlens.dashboard.models.pin.model import Pin

        baker.make(Pin, profile=self.owner, location=self.location)

        _propagate_exact_site_to_nearby_locations(self.location, self.exact_site)

        # No cache row was created for self.location by this call - fetch() (not
        # propagation) is what caches the originating Location's own match.
        self.assertIsNone(self._cached_data(self.location))


class DetailPanelFetchPropagatesExactSiteTests(TestCase):
    """End-to-end: EpaEchoDetailPanelSource.fetch() must propagate a newly-found
    exact-site match to nearby pinned Locations, not just cache it for itself."""

    def setUp(self) -> None:
        super().setUp()
        self.source = EpaEchoDetailPanelSource()
        self.owner = baker.make(User).profile
        self.location: Location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.pin: Pin = baker.make_recipe("dashboard.pin", profile=self.owner, location=self.location)

    def test_fetch_propagates_to_a_nearby_pinned_location(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache
        from urbanlens.dashboard.models.pin.model import Pin as PinModel

        neighbor: Location = baker.make("dashboard.Location", latitude=40.0005, longitude=-74.0)
        baker.make(PinModel, profile=self.owner, location=neighbor)
        exact_site = {"name": "Old Mill Factory", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0, "longitude": -74.0}

        with mock.patch(
            "urbanlens.dashboard.plugins.builtin.epa_echo._fetch_epa_echo_data",
            return_value={"facilities": [], "exact_site": exact_site},
        ):
            self.source.fetch(self.pin)

        neighbor_row = LocationCache.objects.get(location=neighbor, source="epa_echo")
        self.assertEqual(neighbor_row.data["exact_site"], exact_site)


class FacilityFromPoiKeyNamesTests(SimpleTestCase):
    """The plugin's field names have to match the ones REData actually emits.

    Two were guessed before REData's `epa_echo` provider module existed - the
    docstring said so - and guessed wrong: `quarters_in_noncompliance` for
    `quarters_with_violation`, and `last_inspection` for `last_inspection_date`.
    Nothing failed. Every regulated facility simply printed "last inspected no
    recorded inspection" and no non-compliance count, on a live page, for as
    long as the guess stood.

    The fixture below is REData's real shape, read from
    `REData/src/redata/parcels/services/epa_echo/lookup.py`'s `attributes`
    block - not the plugin's own shape, which is what made the original
    mismatch invisible to tests.
    """

    def _redata_row(self) -> dict:
        return {
            "provider": "epa_echo",
            "external_id": "110000123456",
            "name": "Old Mill Factory",
            "category": "Regulated facility",
            "description": "123 Main St, Albany, NY, 12207",
            "url": "https://echo.epa.gov/detailed-facility-report?fid=110000123456",
            "latitude": 42.65,
            "longitude": -73.75,
            "attributes": {
                "quarters_with_violation": "2",
                "inspection_count": "7",
                "last_inspection_date": "2025-01-01",
                "programs": "",
                "naics": "331110",
                "sic": "3312",
                "registry_id": "110000123456",
                "address": "123 Main St, Albany, NY, 12207",
                "compliance_status": "Significant Violator",
                "significant_violator": True,
                "active": True,
                "search_lat": 42.65,
                "search_lon": -73.75,
            },
        }

    def test_the_compliance_fields_are_read_from_the_keys_redata_emits(self) -> None:
        facility = _facility_from_poi(self._redata_row())

        self.assertEqual(facility["last_inspection"], "2025-01-01")
        self.assertEqual(facility["quarters_in_noncompliance"], "2")
        self.assertEqual(facility["inspection_count"], "7")

    def test_the_rest_of_the_shape_is_unchanged(self) -> None:
        facility = _facility_from_poi(self._redata_row())

        self.assertEqual(facility["registry_id"], "110000123456")
        self.assertEqual(facility["name"], "Old Mill Factory")
        self.assertEqual(facility["address"], "123 Main St, Albany, NY, 12207")
        self.assertEqual(facility["compliance_status"], "Significant Violator")
        self.assertTrue(facility["significant_violator"])

    def test_a_row_with_no_attributes_degrades_rather_than_raising(self) -> None:
        facility = _facility_from_poi({"external_id": "R1", "name": "Bare"})

        self.assertEqual(facility["last_inspection"], "")
        self.assertIsNone(facility["quarters_in_noncompliance"])

    def test_the_card_says_when_it_was_inspected(self) -> None:
        """End to end: the rendered fact used to read "last inspected no recorded inspection"."""
        source = EpaEchoDetailPanelSource()
        pin = None  # render_context does not touch the pin for this branch

        context = source.render_context(pin, {"exact_site": _facility_from_poi(self._redata_row())})

        assert context is not None
        self.assertIn("last inspected 2025-01-01", context["facts"][0]["text"])
        self.assertIn("2 quarter(s) in noncompliance", context["facts"][0]["text"])
