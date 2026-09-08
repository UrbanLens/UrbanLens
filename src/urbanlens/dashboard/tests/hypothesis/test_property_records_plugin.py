"""Tests for the property_records plugin's panel rendering and OFFICIAL owner/sale writer.

Covers:
- PropertyRecordsPanelSource.render_context: the found-record card, the
  manual-only pointer card, and the quiet-204 cases.
- _write_official_owners_and_sales: creates OwnerSource.OFFICIAL WikiOwner/
  WikiPropertySale rows, never duplicates them on a repeat fetch, and never
  overwrites a pre-existing (e.g. user-entered) owner of the same name.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale
from urbanlens.dashboard.plugins.builtin.property_records import (
    PropertyRecordsPanelSource,
    _coverage_worth_calling,
    _demographics_rows,
    _write_official_owners_and_sales,
)


class PanelRenderContextTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = PropertyRecordsPanelSource()
        self.pin = None  # No viewer to resolve - see test_an_unresolvable_viewer_never_gets_the_owner_name.

    def test_empty_data_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {}))

    def test_no_data_yields_none(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, None))

    def test_generic_unavailable_reason_yields_none(self) -> None:
        data = {"available": False, "reason": "no_data_found", "message": "nothing found"}
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_unresearched_reason_yields_none(self) -> None:
        data = {"available": False, "reason": "unresearched", "message": "no source configured"}
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_manual_only_with_links_renders_a_card(self) -> None:
        data = {
            "available": False,
            "reason": "manual_only",
            "message": "Call the assessor.",
            "links": {"assessor_url": "https://example.gov/assessor"},
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], ["Manual lookup required"])
        self.assertTrue(any(entry["href"] == "https://example.gov/assessor" for entry in ctx["meta"]))

    def test_manual_only_with_no_links_or_message_yields_none(self) -> None:
        data = {"available": False, "reason": "manual_only"}
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_captcha_blocked_renders_the_manual_lookup_card(self) -> None:
        data = {
            "available": False,
            "reason": "blocked",
            "message": "CAPTCHA-protected search.",
            "links": {"assessor_url": "https://example.gov/assessor"},
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], ["Manual lookup required"])
        self.assertTrue(any(entry["href"] == "https://example.gov/assessor" for entry in ctx["meta"]))

    def test_an_unresolvable_viewer_never_gets_the_owner_name(self) -> None:
        """The owner name is subscriber-only county assessor data, so a call
        with no viewer to check entitlement against withholds it rather than
        publishing a private individual's name by default. Which viewers *do*
        see it is covered in ``test_property_owner_access.py``, which has real
        pins to check against."""
        data = {
            "available": True,
            "situs_address": "123 Main St",
            "apn": "1-2-3",
            "owner_name": ["Jane Smith"],
            "land_use_code": None,
            "lot_size_sqft": None,
            "building_sqft": None,
            "year_built": None,
            "assessed_value": None,
            "market_value": None,
            "tax_history": [],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIsNone(ctx["heading_name"])
        self.assertEqual(ctx["chips"], ["Owner on record - subscribers only"])

    def test_delinquent_tax_history_adds_a_chip(self) -> None:
        data = {
            "available": True,
            "situs_address": "",
            "apn": "",
            "owner_name": [],
            "land_use_code": None,
            "lot_size_sqft": None,
            "building_sqft": None,
            "year_built": None,
            "assessed_value": None,
            "market_value": None,
            "tax_history": [{"year": 2024, "delinquent": True}],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("Delinquent taxes", ctx["chips"])

    def _base_available_data(self, **overrides) -> dict:
        data = {
            "available": True,
            "situs_address": "",
            "apn": "",
            "owner_name": [],
            "land_use_code": None,
            "lot_size_sqft": None,
            "building_sqft": None,
            "year_built": None,
            "assessed_value": None,
            "market_value": None,
            "tax_history": [],
        }
        data.update(overrides)
        return data

    def test_zoning_tax_district_school_district_are_shown(self) -> None:
        data = self._base_available_data(zoning_code="R-1", tax_district="12", school_district="Unified 5")
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Zoning"], "R-1")
        self.assertEqual(labels_values["Tax district"], "12")
        self.assertEqual(labels_values["School district"], "Unified 5")

    def test_subdivision_and_neighborhood_are_shown(self) -> None:
        data = self._base_available_data(subdivision_name="Oak Hills", neighborhood="Downtown")
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Subdivision"], "Oak Hills")
        self.assertEqual(labels_values["Neighborhood"], "Downtown")

    def test_prior_parcel_ids_are_shown(self) -> None:
        data = self._base_available_data(prior_parcel_ids=["OLD-1", "OLD-2"])
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Prior parcel ID"], "OLD-1, OLD-2")

    def test_exemption_type_with_deferred_value_combines_into_one_line(self) -> None:
        data = self._base_available_data(exemption_type="Agricultural", deferred_value=15000.0)
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Exemption"], "Agricultural ($15,000 deferred)")

    def test_exemption_type_without_deferred_value_shows_alone(self) -> None:
        data = self._base_available_data(exemption_type="Homestead")
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Exemption"], "Homestead")

    def test_building_characteristics_are_shown(self) -> None:
        data = self._base_available_data(
            building_characteristics={
                "stories": 2.0,
                "roof_material": "Metal",
                "wall_material": "Brick",
                "garage": "Attached 2-car",
                "heating_type": "Forced Air",
                "quality": "Average",
                "condition": "Good",
                "building_count": 2,
                "outbuilding_value": 5000.0,
            },
        )
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Stories"], "2")
        self.assertEqual(labels_values["Roof"], "Metal")
        self.assertEqual(labels_values["Exterior walls"], "Brick")
        self.assertEqual(labels_values["Garage"], "Attached 2-car")
        self.assertEqual(labels_values["Heating"], "Forced Air")
        self.assertEqual(labels_values["Building quality"], "Average")
        self.assertEqual(labels_values["Building condition"], "Good")
        self.assertEqual(labels_values["Buildings on parcel"], 2)
        self.assertEqual(labels_values["Outbuilding value"], "$5,000")

    def test_single_building_count_is_not_shown_as_a_redundant_fact(self) -> None:
        data = self._base_available_data(building_characteristics={"building_count": 1})
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = {entry["label"] for entry in ctx["meta"]}
        self.assertNotIn("Buildings on parcel", labels)

    def test_no_building_characteristics_adds_no_meta_entries(self) -> None:
        data = self._base_available_data(building_characteristics=None)
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = {entry["label"] for entry in ctx["meta"]}
        self.assertNotIn("Stories", labels)

    def test_parcel_geometry_adds_a_boundary_available_chip(self) -> None:
        data = self._base_available_data(parcel_geometry={"format": "esri_rings", "rings": [[[1, 2], [3, 4], [5, 6]]]})
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("Boundary available", ctx["chips"])

    def test_no_parcel_geometry_does_not_add_the_chip(self) -> None:
        data = self._base_available_data(parcel_geometry=None)
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertNotIn("Boundary available", ctx["chips"])

    def test_debug_count_reflects_availability(self) -> None:
        self.assertEqual(self.source.debug_count({"available": True}), 1)
        self.assertEqual(self.source.debug_count({"available": False, "reason": "manual_only"}), 1)
        self.assertEqual(self.source.debug_count({"available": False, "reason": "blocked"}), 1)
        self.assertEqual(self.source.debug_count({"available": False, "reason": "no_data_found"}), 0)
        self.assertEqual(self.source.debug_count({}), 0)

    def test_demographics_are_rendered_as_meta_rows(self) -> None:
        data = self._base_available_data(
            demographics={
                "population": 295911,
                "median_household_income": "81234.00",
                "median_home_value": "358900.00",
                "median_gross_rent": "1345.00",
                "percent_owner_occupied": "68.20",
                "percent_renter_occupied": "31.80",
            },
        )
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels_values = {entry["label"]: entry["value"] for entry in ctx["meta"]}
        self.assertEqual(labels_values["Neighborhood population"], "295,911")
        self.assertEqual(labels_values["Median household income"], "$81,234")
        self.assertEqual(labels_values["Median home value"], "$358,900")
        self.assertEqual(labels_values["Median gross rent"], "$1,345/mo")
        self.assertEqual(labels_values["Owner/renter occupied"], "68% / 32%")

    def test_no_demographics_adds_no_meta_entries(self) -> None:
        data = self._base_available_data(demographics=None)
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = {entry["label"] for entry in ctx["meta"]}
        self.assertNotIn("Neighborhood population", labels)

    def test_demographics_missing_key_is_the_same_as_none(self) -> None:
        """The 503/best-effort case: ``_fetch_payload`` never sets the key at all."""
        data = self._base_available_data()
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = {entry["label"] for entry in ctx["meta"]}
        self.assertNotIn("Neighborhood population", labels)

    def test_containing_park_adds_a_chip(self) -> None:
        data = self._base_available_data(
            containing_park={"park_code": "yell", "full_name": "Yellowstone National Park"}
        )
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("Situated within Yellowstone National Park", ctx["chips"])

    def test_no_containing_park_adds_no_chip(self) -> None:
        data = self._base_available_data(containing_park=None)
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertFalse(any(chip.startswith("Situated within") for chip in ctx["chips"]))


class CoverageWorthCallingTests(SimpleTestCase):
    """Unit tests for the coverage-precheck gate that ``_fetch_payload`` applies to
    the assessments/sale_records calls only (see the task's own PART A note that
    liens/tax_payments have no coverage-registry domain to gate on)."""

    def test_available_false_means_skip(self) -> None:
        coverage = {"assessments": {"available": False, "reason": "outside every coverage area"}}
        self.assertFalse(_coverage_worth_calling(coverage, "assessments"))

    def test_available_true_means_call(self) -> None:
        coverage = {"assessments": {"available": True, "reason": "covered"}}
        self.assertTrue(_coverage_worth_calling(coverage, "assessments"))

    def test_missing_domain_defaults_to_call(self) -> None:
        """liens/tax_payments are never coverage keys at all - must default to calling."""
        self.assertTrue(_coverage_worth_calling({}, "liens"))

    def test_a_failed_precheck_empty_dict_defaults_to_call(self) -> None:
        self.assertTrue(_coverage_worth_calling({}, "assessments"))
        self.assertTrue(_coverage_worth_calling({}, "sale_records"))


class DemographicsRowsTests(SimpleTestCase):
    def test_none_yields_no_rows(self) -> None:
        self.assertEqual(_demographics_rows(None), [])

    def test_partial_data_omits_missing_fields(self) -> None:
        rows = _demographics_rows({"population": 1000})
        labels = {row["label"] for row in rows}
        self.assertIn("Neighborhood population", labels)
        self.assertNotIn("Median household income", labels)

    def test_owner_renter_split_needs_both_percentages(self) -> None:
        rows = _demographics_rows({"percent_owner_occupied": "68.20"})
        labels = {row["label"] for row in rows}
        self.assertNotIn("Owner/renter occupied", labels)


class FetchPayloadTransientErrorTests(TestCase):
    """A transient source outage must propagate, never be written to the cache as a durable fact."""

    _PATCH_TARGET = "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway"

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make("dashboard.Location")

    def test_source_error_reraises_instead_of_returning_a_cacheable_payload(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
            REASON_SOURCE_ERROR,
            PropertyRecordsUnavailableError,
        )

        error = PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "down")
        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            mock_gateway_cls.return_value.lookup_parcel.side_effect = error
            with self.assertRaises(PropertyRecordsUnavailableError):
                _fetch_payload(self.location, 42.65, -73.75)

    def test_permanent_reason_returns_a_cacheable_unavailable_payload_with_links(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
            REASON_MANUAL_ONLY,
            PropertyRecordsUnavailableError,
        )

        error = PropertyRecordsUnavailableError(
            REASON_MANUAL_ONLY, "Call the assessor.", links={"assessor_url": "https://example.gov/assessor"}
        )
        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            mock_gateway_cls.return_value.lookup_parcel.side_effect = error
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["available"], False)
        self.assertEqual(payload["reason"], REASON_MANUAL_ONLY)
        self.assertEqual(payload["links"], {"assessor_url": "https://example.gov/assessor"})

    def test_successful_lookup_marks_the_payload_available(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            mock_gateway_cls.return_value.lookup_parcel.return_value = {
                "situs_address": "123 Main St",
                "owner_name": ["Jane Smith"],
            }
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["available"], True)
        self.assertEqual(payload["owner_name"], ["Jane Smith"])

    def test_the_locations_address_is_passed_through_as_the_situs_search_key(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        # Location.address is a read-only property composed from the component
        # fields; assigning to it (as this test used to) raises AttributeError.
        # Set the components and let it compose, then assert on that value
        # rather than a hard-coded string, so the test stays about pass-through
        # instead of pinning the composition format.
        self.location.street_number = "123"
        self.location.route = "Main St"
        expected_address = self.location.address
        self.assertIsNotNone(expected_address)

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            mock_gateway_cls.return_value.lookup_parcel.return_value = {}
            _fetch_payload(self.location, 42.65, -73.75)
        mock_gateway_cls.return_value.lookup_parcel.assert_called_once_with(
            42.65, -73.75, situs_address=expected_address
        )


class FetchPayloadSupplementaryCallsTests(TestCase):
    """The coverage-gated assessments/sale_records calls, the two ungated
    liens/tax_payments calls, and the demographics/national-parks best-effort
    calls - all once ``lookup_parcel`` resolves a uuid."""

    _PATCH_TARGET = "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway"

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make("dashboard.Location")

    def _mock_gateway(self, mock_gateway_cls, **overrides):
        """Configure the shared gateway mock with sane empty defaults, overridable per-test."""
        gateway = mock_gateway_cls.return_value
        gateway.lookup_parcel.return_value = {"uuid": "parcel-1"}
        gateway.lookup_coverage.return_value = overrides.get("coverage", {})
        gateway.lookup_assessments.return_value = overrides.get("assessments", [])
        gateway.lookup_sale_records.return_value = overrides.get("sale_records", [])
        gateway.lookup_liens.return_value = overrides.get("liens", [])
        gateway.lookup_tax_payments.return_value = overrides.get("tax_payments", [])
        gateway.lookup_demographics.return_value = overrides.get("demographics")
        gateway.lookup_national_parks.return_value = overrides.get("national_parks", {})
        return gateway

    # -- PART A: coverage gating ------------------------------------------------

    def test_coverage_unavailable_skips_the_assessments_call(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(
                mock_gateway_cls, coverage={"assessments": {"available": False, "reason": "no coverage"}}
            )
            _fetch_payload(self.location, 42.65, -73.75)
        gateway.lookup_assessments.assert_not_called()

    def test_coverage_unavailable_skips_the_sale_records_call(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(
                mock_gateway_cls, coverage={"sale_records": {"available": False, "reason": "no coverage"}}
            )
            _fetch_payload(self.location, 42.65, -73.75)
        gateway.lookup_sale_records.assert_not_called()

    def test_coverage_available_still_calls_assessments_and_sale_records(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(
                mock_gateway_cls,
                coverage={
                    "assessments": {"available": True, "reason": "covered"},
                    "sale_records": {"available": True, "reason": "covered"},
                },
            )
            _fetch_payload(self.location, 42.65, -73.75)
        gateway.lookup_assessments.assert_called_once_with("parcel-1")
        gateway.lookup_sale_records.assert_called_once_with("parcel-1")

    def test_a_failed_coverage_precheck_falls_back_to_calling_both(self) -> None:
        """Coverage is an optimization, not a dependency - a failure must not lose the card's
        most useful supplementary sections."""
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
            REASON_SOURCE_ERROR,
            PropertyRecordsUnavailableError,
        )

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(mock_gateway_cls)
            gateway.lookup_coverage.side_effect = PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "down")
            _fetch_payload(self.location, 42.65, -73.75)
        gateway.lookup_assessments.assert_called_once_with("parcel-1")
        gateway.lookup_sale_records.assert_called_once_with("parcel-1")

    def test_liens_and_tax_payments_are_called_regardless_of_coverage(self) -> None:
        """liens/tax_payments are not coverage-registry domains, so coverage saying
        unavailable for everything else must not skip them."""
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(
                mock_gateway_cls,
                coverage={
                    "assessments": {"available": False, "reason": "no coverage"},
                    "sale_records": {"available": False, "reason": "no coverage"},
                },
            )
            _fetch_payload(self.location, 42.65, -73.75)
        gateway.lookup_liens.assert_called_once_with("parcel-1")
        gateway.lookup_tax_payments.assert_called_once_with("parcel-1")

    # -- PART B: demographics -----------------------------------------------------

    def test_demographics_are_included_when_available(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            self._mock_gateway(mock_gateway_cls, demographics={"population": 1000})
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["demographics"], {"population": 1000})

    def test_null_demographics_are_not_included(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            self._mock_gateway(mock_gateway_cls, demographics=None)
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertNotIn("demographics", payload)

    def test_a_503_demographics_failure_is_swallowed_not_raised(self) -> None:
        """The endpoint 503s wholesale without ``RD_US_CENSUS_API_KEY`` configured server-side -
        a supplementary-source failure like any other, must not blank the card."""
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import PropertyRecordsUnavailableError

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(mock_gateway_cls)
            gateway.lookup_demographics.side_effect = PropertyRecordsUnavailableError(
                "census_data_api_unavailable", "RD_US_CENSUS_API_KEY is not configured."
            )
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["available"], True)
        self.assertNotIn("demographics", payload)

    def test_a_rate_limited_demographics_failure_is_also_swallowed(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
            REASON_RATE_LIMITED,
            PropertyRecordsUnavailableError,
        )

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(mock_gateway_cls)
            gateway.lookup_demographics.side_effect = PropertyRecordsUnavailableError(
                REASON_RATE_LIMITED, "rate limited"
            )
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["available"], True)
        self.assertNotIn("demographics", payload)

    # -- PART C: national parks -----------------------------------------------------

    def test_containing_park_is_included_when_present(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            self._mock_gateway(
                mock_gateway_cls,
                national_parks={
                    "containing_park": {"full_name": "Yellowstone National Park"},
                    "nearby_parks": [{"full_name": "Grand Teton National Park"}],
                },
            )
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["containing_park"]["full_name"], "Yellowstone National Park")
        self.assertNotIn("nearby_parks", payload)

    def test_no_containing_park_is_not_included(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            self._mock_gateway(mock_gateway_cls, national_parks={"containing_park": None, "nearby_parks": []})
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertNotIn("containing_park", payload)

    def test_a_failed_national_parks_lookup_is_swallowed_not_raised(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
            REASON_SOURCE_ERROR,
            PropertyRecordsUnavailableError,
        )

        with mock.patch(self._PATCH_TARGET) as mock_gateway_cls:
            gateway = self._mock_gateway(mock_gateway_cls)
            gateway.lookup_national_parks.side_effect = PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "down")
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["available"], True)
        self.assertNotIn("containing_park", payload)


class WriteOfficialOwnersAndSalesTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make("dashboard.Location")

    def test_creates_an_official_owner(self) -> None:
        _write_official_owners_and_sales(self.location, {"owner_name": ["Jane Smith"]})
        owner = WikiOwner.objects.for_location(self.location).get(name="Jane Smith")
        self.assertEqual(owner.source, OwnerSource.OFFICIAL)

    def test_repeat_fetch_does_not_duplicate_the_owner(self) -> None:
        _write_official_owners_and_sales(self.location, {"owner_name": ["Jane Smith"]})
        _write_official_owners_and_sales(self.location, {"owner_name": ["Jane Smith"]})
        self.assertEqual(WikiOwner.objects.for_location(self.location).filter(name="Jane Smith").count(), 1)

    def test_existing_user_owner_of_the_same_name_is_reused_not_duplicated(self) -> None:
        WikiOwner.objects.create(name="Jane Smith", source=OwnerSource.USER).locations.add(self.location)
        _write_official_owners_and_sales(self.location, {"owner_name": ["Jane Smith"]})
        owners = WikiOwner.objects.for_location(self.location).filter(name="Jane Smith")
        self.assertEqual(owners.count(), 1)
        self.assertEqual(owners.first().source, OwnerSource.USER)

    def test_mailing_address_only_applied_to_a_newly_created_owner(self) -> None:
        _write_official_owners_and_sales(
            self.location, {"owner_name": ["Jane Smith"], "owner_mailing_address": "PO Box 1"}
        )
        owner = WikiOwner.objects.for_location(self.location).get(name="Jane Smith")
        self.assertEqual(owner.address, "PO Box 1")

    def test_blank_owner_names_are_skipped(self) -> None:
        _write_official_owners_and_sales(self.location, {"owner_name": ["", "  ", "Jane Smith"]})
        self.assertEqual(WikiOwner.objects.for_location(self.location).count(), 1)

    def test_creates_a_sale_with_price_and_date(self) -> None:
        _write_official_owners_and_sales(
            self.location,
            {
                "sales_history": [
                    {"date": "2020-06-15", "price": 250000, "grantor": "Old Owner", "grantee": "New Owner"}
                ]
            },
        )
        sale = WikiPropertySale.objects.for_location(self.location).get()
        self.assertEqual(str(sale.sale_price), "250000.00")
        self.assertEqual(sale.sale_date.isoformat(), "2020-06-15")
        self.assertEqual(sale.source, OwnerSource.OFFICIAL)
        self.assertEqual(list(sale.previous_owners.values_list("name", flat=True)), ["Old Owner"])
        self.assertEqual(list(sale.new_owners.values_list("name", flat=True)), ["New Owner"])

    def test_repeat_fetch_does_not_duplicate_the_sale(self) -> None:
        payload = {
            "sales_history": [{"date": "2020-06-15", "price": 250000, "grantor": "Old Owner", "grantee": "New Owner"}]
        }
        _write_official_owners_and_sales(self.location, payload)
        _write_official_owners_and_sales(self.location, payload)
        self.assertEqual(WikiPropertySale.objects.for_location(self.location).count(), 1)

    def test_sale_with_no_date_and_no_price_is_skipped(self) -> None:
        _write_official_owners_and_sales(
            self.location, {"sales_history": [{"grantor": "Old Owner", "grantee": "New Owner"}]}
        )
        self.assertEqual(WikiPropertySale.objects.for_location(self.location).count(), 0)

    def test_negative_price_is_dropped_not_saved_negative(self) -> None:
        _write_official_owners_and_sales(self.location, {"sales_history": [{"date": "2020-06-15", "price": -100}]})
        sale = WikiPropertySale.objects.for_location(self.location).get()
        self.assertIsNone(sale.sale_price)

    def test_empty_payload_creates_nothing(self) -> None:
        _write_official_owners_and_sales(self.location, {})
        self.assertEqual(WikiOwner.objects.for_location(self.location).count(), 0)
        self.assertEqual(WikiPropertySale.objects.for_location(self.location).count(), 0)
