"""Tests for the EPA ECHO plugin's exact-site/nearby-list split.

Covers:
- EpaEchoDetailPanelSource shows an unconditional card when a facility's DFR
  coordinates are close enough to the pin's own to plausibly BE that pin, and
  204s (renders nothing) otherwise.
- EpaEchoNearbyPanelSource lists nearby facilities, excluding whichever one
  was matched as the exact site (it already has its own card).
- EpaFacilityNameProvider only suggests a name when an exact-site match exists.
- _fetch_epa_echo_data's distance-based exact-match logic against a handful
  of DFR candidates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.plugins.builtin.epa_echo import (
    _EXACT_MATCH_BUDGET_SECONDS,
    EpaEchoDetailPanelSource,
    EpaEchoNearbyPanelSource,
    EpaFacilityNameProvider,
    _fetch_epa_echo_data,
    _miles_between,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin


class MilesBetweenTests(TestCase):
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
        data = {"exact_site": {"name": "Old Mill Factory", "address": "123 Main St", "registry_id": "R1", "programs": []}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Old Mill Factory")

    def test_footer_link_uses_the_detailed_facility_report_url(self) -> None:
        data = {"exact_site": {"name": "Old Mill Factory", "address": "123 Main St", "registry_id": "R123", "programs": []}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["footer_link"]["url"], "https://echo.epa.gov/detailed-facility-report?fid=R123")

    def test_significant_noncompliance_surfaces_as_a_chip_and_meta_entry(self) -> None:
        data = {
            "exact_site": {
                "name": "Old Mill Factory",
                "address": "123 Main St",
                "registry_id": "R1",
                "programs": [{"statute": "RCRA", "quarters_in_significant_noncompliance": "2", "formal_actions": "1", "total_penalties": "$500", "last_inspection": "2025-01-01"}],
            },
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("Significant noncompliance", ctx["chips"])
        self.assertTrue(any(entry["label"] == "Significant noncompliance" and "RCRA" in entry["value"] for entry in ctx["meta"]))

    def test_clean_compliance_history_has_no_danger_chip(self) -> None:
        data = {
            "exact_site": {
                "name": "Old Mill Factory",
                "address": "123 Main St",
                "registry_id": "R1",
                "programs": [{"statute": "RCRA", "quarters_in_significant_noncompliance": "0", "formal_actions": "0", "total_penalties": "$0", "last_inspection": "2025-01-01"}],
            },
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], [])

    def test_missing_registry_id_falls_back_to_generic_echo_link(self) -> None:
        data = {"exact_site": {"name": "Old Mill Factory", "address": "123 Main St", "registry_id": "", "programs": []}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["footer_link"]["url"], "https://echo.epa.gov/")


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
            "exact_site": {"registry_id": "RA", "name": "Exact Match Facility", "address": "1 A St", "programs": []},
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
            "exact_site": {"registry_id": "RA", "name": "Exact Match Facility", "address": "1 A St", "programs": []},
        }
        self.assertIsNone(self.source.render_context(self.pin, data))


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
    """_fetch_epa_echo_data's distance-based exact-match selection, against a mocked gateway."""

    def setUp(self) -> None:
        super().setUp()
        self.pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=40.0, longitude=-74.0),
        )

    def _gateway(self, *, facilities, detail_by_registry_id):
        gateway = mock.Mock()
        gateway.get_nearby_facilities.return_value = facilities
        gateway.get_facility_detail.side_effect = detail_by_registry_id.get
        return gateway

    def test_no_facilities_returns_no_exact_site(self) -> None:
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=self._gateway(facilities=[], detail_by_registry_id={})):
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_facility_at_pin_coordinates_is_the_exact_site(self) -> None:
        facilities = [{"name": "Right Here Facility", "address": "1 Main St", "registry_id": "R1"}]
        details = {"R1": {"latitude": 40.0, "longitude": -74.0, "programs": []}}
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=self._gateway(facilities=facilities, detail_by_registry_id=details)):
            result = _fetch_epa_echo_data(self.pin)
        assert result["exact_site"] is not None
        self.assertEqual(result["exact_site"]["registry_id"], "R1")
        self.assertEqual(result["exact_site"]["name"], "Right Here Facility")

    def test_facility_far_from_pin_coordinates_is_not_the_exact_site(self) -> None:
        facilities = [{"name": "Far Away Facility", "address": "999 Elsewhere Ave", "registry_id": "R2"}]
        details = {"R2": {"latitude": 41.0, "longitude": -75.0, "programs": []}}  # >0.1mi away
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=self._gateway(facilities=facilities, detail_by_registry_id=details)):
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_closest_of_several_candidates_wins(self) -> None:
        facilities = [
            {"name": "Slightly Off Facility", "address": "2 Main St", "registry_id": "R1"},
            {"name": "Dead On Facility", "address": "1 Main St", "registry_id": "R2"},
        ]
        details = {
            "R1": {"latitude": 40.0005, "longitude": -74.0, "programs": []},
            "R2": {"latitude": 40.0, "longitude": -74.0, "programs": []},
        }
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=self._gateway(facilities=facilities, detail_by_registry_id=details)):
            result = _fetch_epa_echo_data(self.pin)
        assert result["exact_site"] is not None
        self.assertEqual(result["exact_site"]["registry_id"], "R2")

    def test_facility_with_no_registry_id_is_skipped(self) -> None:
        facilities = [{"name": "No Registry Facility", "address": "1 Main St", "registry_id": ""}]
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=self._gateway(facilities=facilities, detail_by_registry_id={})):
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_facility_with_no_detail_coordinates_is_skipped(self) -> None:
        facilities = [{"name": "No Coords Facility", "address": "1 Main St", "registry_id": "R1"}]
        details = {"R1": {"latitude": None, "longitude": None, "programs": []}}
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=self._gateway(facilities=facilities, detail_by_registry_id=details)):
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])

    def test_non_usa_coordinates_short_circuit_without_calling_the_gateway(self) -> None:
        pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=48.8566, longitude=2.3522),  # Paris
        )
        gateway = self._gateway(facilities=[], detail_by_registry_id={})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            result = _fetch_epa_echo_data(pin)
        self.assertEqual(result, {"facilities": [], "exact_site": None})
        gateway.get_nearby_facilities.assert_not_called()

    def test_exceeding_the_wall_clock_budget_stops_checking_further_candidates(self) -> None:
        """Regression guard: a slow/degraded ECHO API must not be able to hold this Celery
        task open anywhere near its 110s soft time limit and starve the ~10 other panel
        fetches sharing the same worker pool on a cold pin page (docker-compose.yml's
        celery-worker concurrency comment) - the loop must bail out on wall-clock time,
        independent of the per-call timeout or candidate count."""
        facilities = [
            {"name": "First Facility", "address": "1 Main St", "registry_id": "R1"},
            {"name": "Second Facility", "address": "2 Main St", "registry_id": "R2"},
        ]
        details = {
            "R1": {"latitude": 41.0, "longitude": -75.0, "programs": []},  # not a match
            "R2": {"latitude": 40.0, "longitude": -74.0, "programs": []},  # would match, if reached
        }
        gateway = self._gateway(facilities=facilities, detail_by_registry_id=details)
        # Call 1 = loop start ("started"). Call 2 = budget check before candidate 1 (still
        # within budget). Call 3 = budget check before candidate 2 (past the budget - stop).
        monotonic_values = [0.0, 1.0, _EXACT_MATCH_BUDGET_SECONDS + 1]
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway),
            mock.patch("urbanlens.dashboard.plugins.builtin.epa_echo.time.monotonic", side_effect=monotonic_values),
        ):
            result = _fetch_epa_echo_data(self.pin)
        self.assertIsNone(result["exact_site"])
        gateway.get_facility_detail.assert_called_once_with("R1")

    def test_within_budget_checks_every_candidate(self) -> None:
        facilities = [
            {"name": "First Facility", "address": "1 Main St", "registry_id": "R1"},
            {"name": "Second Facility", "address": "2 Main St", "registry_id": "R2"},
        ]
        details = {
            "R1": {"latitude": 41.0, "longitude": -75.0, "programs": []},  # not a match
            "R2": {"latitude": 40.0, "longitude": -74.0, "programs": []},  # exact match
        }
        gateway = self._gateway(facilities=facilities, detail_by_registry_id=details)
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway),
            mock.patch("urbanlens.dashboard.plugins.builtin.epa_echo.time.monotonic", return_value=0.0),
        ):
            result = _fetch_epa_echo_data(self.pin)
        assert result["exact_site"] is not None
        self.assertEqual(result["exact_site"]["registry_id"], "R2")
