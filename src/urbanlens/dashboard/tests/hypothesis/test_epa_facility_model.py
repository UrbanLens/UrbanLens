"""Tests for EpaFacility - the persistent, project-wide EPA ECHO facility record.

Covers the model's own upsert/lookup helpers, plus _fetch_epa_echo_data's use
of them to avoid re-spending ECHO's rate-limited API budget on a facility
already fetched while checking some OTHER pin nearby.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.epa_facility.model import EpaFacility
from urbanlens.dashboard.plugins.builtin.epa_echo import _fetch_epa_echo_data

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


class RecordSearchResultTests(TestCase):
    def test_creates_a_new_row(self) -> None:
        EpaFacility.record_search_result("R1", name="Test Facility", address="1 Main St", latitude=40.0, data={"compliance_status": "In compliance"})
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.name, "Test Facility")
        self.assertEqual(entry.latitude, 40.0)
        self.assertEqual(entry.data["compliance_status"], "In compliance")
        self.assertIsNone(entry.detail_fetched_at)

    def test_empty_registry_id_is_a_no_op(self) -> None:
        EpaFacility.record_search_result("", name="Test", address="", latitude=None, data={})
        self.assertEqual(EpaFacility.objects.count(), 0)

    def test_does_not_overwrite_an_existing_detail_fetch(self) -> None:
        EpaFacility.record_detail_result("R1", name="Old Name", address="1 Main St", latitude=40.1234, longitude=-74.1234, data={"programs": []})
        EpaFacility.record_search_result("R1", name="New Name From Search", address="1 Main St", latitude=99.0, data={"compliance_status": "In compliance"})
        entry = EpaFacility.objects.get(registry_id="R1")
        # Coordinates from the DFR (more precise) must survive a later search-only sighting.
        self.assertEqual(entry.latitude, 40.1234)
        self.assertIsNotNone(entry.detail_fetched_at)
        # Non-coordinate fields (name, merged data) still update.
        self.assertEqual(entry.name, "New Name From Search")
        self.assertEqual(entry.data["compliance_status"], "In compliance")

    def test_second_search_sighting_merges_data_rather_than_replacing_it(self) -> None:
        EpaFacility.record_search_result("R1", name="Test", address="1 Main St", latitude=40.0, data={"compliance_status": "In compliance"})
        EpaFacility.record_search_result("R1", name="Test", address="1 Main St", latitude=40.0, data={"inspection_count": "3"})
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.data["compliance_status"], "In compliance")
        self.assertEqual(entry.data["inspection_count"], "3")


class RecordDetailResultTests(TestCase):
    def test_creates_a_new_row_with_detail_fetched_at_set(self) -> None:
        entry = EpaFacility.record_detail_result("R1", name="Test Facility", address="1 Main St", latitude=40.1234, longitude=-74.1234, data={"programs": []})
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.latitude, 40.1234)
        self.assertEqual(entry.longitude, -74.1234)

    def test_upgrades_a_search_only_row(self) -> None:
        EpaFacility.record_search_result("R1", name="Test", address="1 Main St", latitude=40.0, data={"compliance_status": "In compliance"})
        EpaFacility.record_detail_result("R1", name="Test", address="1 Main St", latitude=40.1234, longitude=-74.1234, data={"programs": []})
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.longitude, -74.1234)
        # Data from the earlier search sighting survives, merged with the new detail data.
        self.assertEqual(entry.data["compliance_status"], "In compliance")

    def test_re_fetching_detail_overwrites_coordinates(self) -> None:
        EpaFacility.record_detail_result("R1", name="Test", address="1 Main St", latitude=40.0, longitude=-74.0, data={"programs": []})
        EpaFacility.record_detail_result("R1", name="Test", address="1 Main St", latitude=41.0, longitude=-75.0, data={"programs": []})
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.latitude, 41.0)
        self.assertEqual(entry.longitude, -75.0)

    def test_coordinate_less_detail_is_recorded_but_never_clobbers_real_coordinates(self) -> None:
        """A real DFR response with no Permits data (so no coordinates) still
        marks the facility as detail-fetched - it can never be an exact-site
        match, and recording that saves re-fetching it for every nearby pin -
        but its None coordinates must not erase a search-derived latitude or
        a previous richer DFR's coordinates."""
        EpaFacility.record_search_result("R1", name="Test", address="1 Main St", latitude=40.0, data={})
        entry = EpaFacility.record_detail_result("R1", name="Test", address="1 Main St", latitude=None, longitude=None, data={"programs": []})
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.latitude, 40.0)
        self.assertIsNone(entry.longitude)


class KnownDetailsByRegistryIdTests(TestCase):
    def test_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(EpaFacility.known_details_by_registry_id([]), {})

    def test_search_only_rows_are_excluded(self) -> None:
        EpaFacility.record_search_result("R1", name="Test", address="1 Main St", latitude=40.0, data={})
        self.assertEqual(EpaFacility.known_details_by_registry_id(["R1"]), {})

    def test_detail_fetched_rows_are_included(self) -> None:
        EpaFacility.record_detail_result("R1", name="Test", address="1 Main St", latitude=40.0, longitude=-74.0, data={"programs": []})
        result = EpaFacility.known_details_by_registry_id(["R1"])
        self.assertIn("R1", result)
        self.assertEqual(result["R1"].latitude, 40.0)

    def test_unknown_registry_ids_are_absent(self) -> None:
        self.assertEqual(EpaFacility.known_details_by_registry_id(["DOES-NOT-EXIST"]), {})

    def test_blank_ids_in_input_are_ignored(self) -> None:
        EpaFacility.record_detail_result("R1", name="Test", address="1 Main St", latitude=40.0, longitude=-74.0, data={"programs": []})
        result = EpaFacility.known_details_by_registry_id(["R1", "", None])  # type: ignore[list-item]
        self.assertEqual(list(result), ["R1"])


class FetchEpaEchoDataReusesPersistedFacilitiesTests(TestCase):
    """_fetch_epa_echo_data reuses EpaFacility rows already fetched for some
    OTHER pin, instead of spending ECHO's rate-limited API budget again."""

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

    def test_already_known_detail_is_reused_without_an_api_call(self) -> None:
        EpaFacility.record_detail_result("R1", name="Cached Facility", address="1 Main St", latitude=40.0, longitude=-74.0, data={"programs": []})
        facilities = [{"name": "Cached Facility", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0}]
        gateway = self._gateway(facilities=facilities, detail_by_registry_id={})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            result = _fetch_epa_echo_data(self.pin)
        gateway.get_facility_detail.assert_not_called()
        assert result["exact_site"] is not None
        self.assertEqual(result["exact_site"]["registry_id"], "R1")

    def test_search_results_are_persisted_for_future_reuse(self) -> None:
        facilities = [{"name": "New Facility", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0}]
        gateway = self._gateway(facilities=facilities, detail_by_registry_id={"R1": {"latitude": 40.0, "longitude": -74.0, "programs": []}})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            _fetch_epa_echo_data(self.pin)
        self.assertTrue(EpaFacility.objects.filter(registry_id="R1").exists())

    def test_fetched_detail_is_persisted_for_future_reuse(self) -> None:
        facilities = [{"name": "New Facility", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0}]
        gateway = self._gateway(facilities=facilities, detail_by_registry_id={"R1": {"latitude": 40.0, "longitude": -74.0, "programs": []}})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            _fetch_epa_echo_data(self.pin)
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.longitude, -74.0)

    def test_coordinate_less_detail_is_persisted_and_never_re_fetched(self) -> None:
        """A facility whose DFR genuinely has no coordinates must be recorded
        as already-checked - the whole point of the persistent store is that a
        SECOND pin's fetch spends none of ECHO's 5-calls/minute budget
        re-ruling out a facility that can never be an exact-site match."""
        facilities = [{"name": "No Coords Facility", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0}]
        gateway = self._gateway(facilities=facilities, detail_by_registry_id={"R1": {"latitude": None, "longitude": None, "programs": []}})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            _fetch_epa_echo_data(self.pin)
        gateway.get_facility_detail.assert_called_once_with("R1")
        self.assertIsNotNone(EpaFacility.objects.get(registry_id="R1").detail_fetched_at)

        second_gateway = self._gateway(facilities=facilities, detail_by_registry_id={})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=second_gateway):
            result = _fetch_epa_echo_data(self.pin)
        second_gateway.get_facility_detail.assert_not_called()
        self.assertIsNone(result["exact_site"])

    def test_a_transient_detail_failure_is_not_recorded_as_checked(self) -> None:
        """get_facility_detail returning None means a failed/unknown lookup -
        recording THAT as detail-fetched would permanently mark a facility
        coordinate-less off one flaky response. It must stay re-fetchable."""
        facilities = [{"name": "Flaky Facility", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0}]
        gateway = self._gateway(facilities=facilities, detail_by_registry_id={})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            _fetch_epa_echo_data(self.pin)
        self.assertIsNone(EpaFacility.objects.get(registry_id="R1").detail_fetched_at)

    def test_a_stale_pre_existing_row_still_counts_as_known_forever(self) -> None:
        """EpaFacility is reference data, not a time-limited cache - even a very
        old detail_fetched_at must still be reused, not treated as expired."""
        entry = EpaFacility.record_detail_result("R1", name="Old Facility", address="1 Main St", latitude=40.0, longitude=-74.0, data={"programs": []})
        EpaFacility.objects.filter(pk=entry.pk).update(detail_fetched_at=timezone.now() - timedelta(days=3650))
        facilities = [{"name": "Old Facility", "address": "1 Main St", "registry_id": "R1", "latitude": 40.0}]
        gateway = self._gateway(facilities=facilities, detail_by_registry_id={})
        with mock.patch("urbanlens.dashboard.services.apis.locations.epa_echo.EpaEchoGateway", return_value=gateway):
            result = _fetch_epa_echo_data(self.pin)
        gateway.get_facility_detail.assert_not_called()
        assert result["exact_site"] is not None
