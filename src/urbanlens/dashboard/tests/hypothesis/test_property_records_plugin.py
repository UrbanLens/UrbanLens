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
    _write_official_owners_and_sales,
)


class PanelRenderContextTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = PropertyRecordsPanelSource()
        self.pin = None  # render_context doesn't actually use pin for this source.

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
        data = {"available": False, "reason": "manual_only", "message": "Call the assessor.", "links": {"assessor_url": "https://example.gov/assessor"}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], ["Manual lookup required"])
        self.assertTrue(any(entry["href"] == "https://example.gov/assessor" for entry in ctx["meta"]))

    def test_manual_only_with_no_links_or_message_yields_none(self) -> None:
        data = {"available": False, "reason": "manual_only"}
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_captcha_blocked_renders_the_manual_lookup_card(self) -> None:
        data = {"available": False, "reason": "blocked", "message": "CAPTCHA-protected search.", "links": {"assessor_url": "https://example.gov/assessor"}}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["chips"], ["Manual lookup required"])
        self.assertTrue(any(entry["href"] == "https://example.gov/assessor" for entry in ctx["meta"]))

    def test_available_record_shows_owner_and_chips(self) -> None:
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
            "source": {"tier": 1, "provider": "ArcGIS REST", "url": "https://example.gov"},
            "confidence": 0.7,
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["heading_name"], "Jane Smith")
        self.assertIn("Tier 1", ctx["chips"])
        self.assertIn("70% confidence", ctx["chips"])

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
            "source": {"tier": 1, "provider": "ArcGIS REST", "url": ""},
            "confidence": 0.7,
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
            "source": {"tier": 1, "provider": "ArcGIS REST", "url": ""},
            "confidence": 0.7,
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


class FetchPayloadTransientErrorTests(TestCase):
    """A transient source outage must propagate, never be written to the cache as a durable fact."""

    _PATCH_TARGET = "urbanlens.dashboard.services.apis.property_records.orchestrator.get_property_record"

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make("dashboard.Location")

    def test_source_error_reraises_instead_of_returning_a_cacheable_payload(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.orchestrator import REASON_SOURCE_ERROR, PropertyRecordsUnavailableError

        error = PropertyRecordsUnavailableError(REASON_SOURCE_ERROR, "down")
        with mock.patch(self._PATCH_TARGET, side_effect=error), self.assertRaises(PropertyRecordsUnavailableError):
            _fetch_payload(self.location, 42.65, -73.75)

    def test_permanent_reason_returns_a_cacheable_unavailable_payload_with_links(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.plugins.builtin.property_records import _fetch_payload
        from urbanlens.dashboard.services.apis.property_records.orchestrator import REASON_MANUAL_ONLY, PropertyRecordsUnavailableError

        error = PropertyRecordsUnavailableError(REASON_MANUAL_ONLY, "Call the assessor.", links={"assessor_url": "https://example.gov/assessor"})
        with mock.patch(self._PATCH_TARGET, side_effect=error):
            payload = _fetch_payload(self.location, 42.65, -73.75)
        self.assertEqual(payload["available"], False)
        self.assertEqual(payload["reason"], REASON_MANUAL_ONLY)
        self.assertEqual(payload["links"], {"assessor_url": "https://example.gov/assessor"})


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
        _write_official_owners_and_sales(self.location, {"owner_name": ["Jane Smith"], "owner_mailing_address": "PO Box 1"})
        owner = WikiOwner.objects.for_location(self.location).get(name="Jane Smith")
        self.assertEqual(owner.address, "PO Box 1")

    def test_blank_owner_names_are_skipped(self) -> None:
        _write_official_owners_and_sales(self.location, {"owner_name": ["", "  ", "Jane Smith"]})
        self.assertEqual(WikiOwner.objects.for_location(self.location).count(), 1)

    def test_creates_a_sale_with_price_and_date(self) -> None:
        _write_official_owners_and_sales(self.location, {"sales_history": [{"date": "2020-06-15", "price": 250000, "grantor": "Old Owner", "grantee": "New Owner"}]})
        sale = WikiPropertySale.objects.for_location(self.location).get()
        self.assertEqual(str(sale.sale_price), "250000.00")
        self.assertEqual(sale.sale_date.isoformat(), "2020-06-15")
        self.assertEqual(sale.source, OwnerSource.OFFICIAL)
        self.assertEqual(list(sale.previous_owners.values_list("name", flat=True)), ["Old Owner"])
        self.assertEqual(list(sale.new_owners.values_list("name", flat=True)), ["New Owner"])

    def test_repeat_fetch_does_not_duplicate_the_sale(self) -> None:
        payload = {"sales_history": [{"date": "2020-06-15", "price": 250000, "grantor": "Old Owner", "grantee": "New Owner"}]}
        _write_official_owners_and_sales(self.location, payload)
        _write_official_owners_and_sales(self.location, payload)
        self.assertEqual(WikiPropertySale.objects.for_location(self.location).count(), 1)

    def test_sale_with_no_date_and_no_price_is_skipped(self) -> None:
        _write_official_owners_and_sales(self.location, {"sales_history": [{"grantor": "Old Owner", "grantee": "New Owner"}]})
        self.assertEqual(WikiPropertySale.objects.for_location(self.location).count(), 0)

    def test_negative_price_is_dropped_not_saved_negative(self) -> None:
        _write_official_owners_and_sales(self.location, {"sales_history": [{"date": "2020-06-15", "price": -100}]})
        sale = WikiPropertySale.objects.for_location(self.location).get()
        self.assertIsNone(sale.sale_price)

    def test_empty_payload_creates_nothing(self) -> None:
        _write_official_owners_and_sales(self.location, {})
        self.assertEqual(WikiOwner.objects.for_location(self.location).count(), 0)
        self.assertEqual(WikiPropertySale.objects.for_location(self.location).count(), 0)
