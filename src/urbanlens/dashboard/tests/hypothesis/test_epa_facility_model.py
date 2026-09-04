"""Tests for EpaFacility - the persistent, project-wide EPA ECHO facility record.

Covers the model's own upsert/lookup helpers. ``plugins.builtin.epa_echo``'s
``_fetch_epa_echo_data`` calls ``record_detail_result`` for every facility
REData's points-of-interest lookup returns (see
``test_epa_echo_nearby_research.py``'s ``FetchEpaEchoDataExactMatchTests`` for
that integration) - ``record_search_result``/``known_details_by_registry_id``
below are no longer called by that fetch (REData resolves every candidate's
detail in one call, unlike the old direct EPA ECHO API's separate,
rate-limited per-candidate Detailed Facility Report fetch), but remain valid,
tested model-level API.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.epa_facility.model import EpaFacility


class RecordSearchResultTests(TestCase):
    def test_creates_a_new_row(self) -> None:
        EpaFacility.record_search_result(
            "R1", name="Test Facility", address="1 Main St", latitude=40.0, data={"compliance_status": "In compliance"}
        )
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.name, "Test Facility")
        self.assertEqual(entry.latitude, 40.0)
        self.assertEqual(entry.data["compliance_status"], "In compliance")
        self.assertIsNone(entry.detail_fetched_at)

    def test_empty_registry_id_is_a_no_op(self) -> None:
        EpaFacility.record_search_result("", name="Test", address="", latitude=None, data={})
        self.assertEqual(EpaFacility.objects.count(), 0)

    def test_does_not_overwrite_an_existing_detail_fetch(self) -> None:
        EpaFacility.record_detail_result(
            "R1",
            name="Old Name",
            address="1 Main St",
            latitude=40.1234,
            longitude=-74.1234,
            data={"compliance_status": "In compliance"},
        )
        EpaFacility.record_search_result(
            "R1",
            name="New Name From Search",
            address="1 Main St",
            latitude=99.0,
            data={"compliance_status": "In compliance"},
        )
        entry = EpaFacility.objects.get(registry_id="R1")
        # Coordinates from the DFR (more precise) must survive a later search-only sighting.
        self.assertEqual(entry.latitude, 40.1234)
        self.assertIsNotNone(entry.detail_fetched_at)
        # Non-coordinate fields (name, merged data) still update.
        self.assertEqual(entry.name, "New Name From Search")
        self.assertEqual(entry.data["compliance_status"], "In compliance")

    def test_second_search_sighting_merges_data_rather_than_replacing_it(self) -> None:
        EpaFacility.record_search_result(
            "R1", name="Test", address="1 Main St", latitude=40.0, data={"compliance_status": "In compliance"}
        )
        EpaFacility.record_search_result(
            "R1", name="Test", address="1 Main St", latitude=40.0, data={"inspection_count": "3"}
        )
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.data["compliance_status"], "In compliance")
        self.assertEqual(entry.data["inspection_count"], "3")


class RecordDetailResultTests(TestCase):
    def test_creates_a_new_row_with_detail_fetched_at_set(self) -> None:
        entry = EpaFacility.record_detail_result(
            "R1",
            name="Test Facility",
            address="1 Main St",
            latitude=40.1234,
            longitude=-74.1234,
            data={"compliance_status": "In compliance"},
        )
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.latitude, 40.1234)
        self.assertEqual(entry.longitude, -74.1234)

    def test_upgrades_a_search_only_row(self) -> None:
        EpaFacility.record_search_result(
            "R1", name="Test", address="1 Main St", latitude=40.0, data={"compliance_status": "In compliance"}
        )
        EpaFacility.record_detail_result(
            "R1",
            name="Test",
            address="1 Main St",
            latitude=40.1234,
            longitude=-74.1234,
            data={"significant_violator": False},
        )
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertIsNotNone(entry.detail_fetched_at)
        self.assertEqual(entry.longitude, -74.1234)
        # Data from the earlier search sighting survives, merged with the new detail data.
        self.assertEqual(entry.data["compliance_status"], "In compliance")

    def test_re_fetching_detail_overwrites_coordinates(self) -> None:
        EpaFacility.record_detail_result(
            "R1", name="Test", address="1 Main St", latitude=40.0, longitude=-74.0, data={}
        )
        EpaFacility.record_detail_result(
            "R1", name="Test", address="1 Main St", latitude=41.0, longitude=-75.0, data={}
        )
        entry = EpaFacility.objects.get(registry_id="R1")
        self.assertEqual(entry.latitude, 41.0)
        self.assertEqual(entry.longitude, -75.0)

    def test_coordinate_less_detail_is_recorded_but_never_clobbers_real_coordinates(self) -> None:
        """A REData row with no coordinates for a facility still marks it as
        detail-fetched - it can never be an exact-site match, and recording
        that fact saves re-fetching it for every nearby pin - but its None
        coordinates must not erase a search-derived latitude or a previous
        richer detail's coordinates."""
        EpaFacility.record_search_result("R1", name="Test", address="1 Main St", latitude=40.0, data={})
        entry = EpaFacility.record_detail_result(
            "R1", name="Test", address="1 Main St", latitude=None, longitude=None, data={}
        )
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
        EpaFacility.record_detail_result(
            "R1", name="Test", address="1 Main St", latitude=40.0, longitude=-74.0, data={}
        )
        result = EpaFacility.known_details_by_registry_id(["R1"])
        self.assertIn("R1", result)
        self.assertEqual(result["R1"].latitude, 40.0)

    def test_unknown_registry_ids_are_absent(self) -> None:
        self.assertEqual(EpaFacility.known_details_by_registry_id(["DOES-NOT-EXIST"]), {})

    def test_blank_ids_in_input_are_ignored(self) -> None:
        EpaFacility.record_detail_result(
            "R1", name="Test", address="1 Main St", latitude=40.0, longitude=-74.0, data={}
        )
        result = EpaFacility.known_details_by_registry_id(["R1", "", None])  # type: ignore[list-item]
        self.assertEqual(list(result), ["R1"])

    def test_a_stale_row_still_counts_as_known_forever(self) -> None:
        """EpaFacility is reference data, not a time-limited cache - even a very
        old detail_fetched_at must still be reported as known, not expired."""
        entry = EpaFacility.record_detail_result(
            "R1", name="Old Facility", address="1 Main St", latitude=40.0, longitude=-74.0, data={}
        )
        EpaFacility.objects.filter(pk=entry.pk).update(detail_fetched_at=timezone.now() - timedelta(days=3650))
        result = EpaFacility.known_details_by_registry_id(["R1"])
        self.assertIn("R1", result)
