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

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.property_owner.meta import OwnerSource
from urbanlens.dashboard.models.property_owner.model import WikiOwner, WikiPropertySale
from urbanlens.dashboard.plugins.builtin.property_records import (
    PropertyRecordsPanelSource,
    _write_official_owners_and_sales,
)


class PanelRenderContextTests(TestCase):
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

    def test_debug_count_reflects_availability(self) -> None:
        self.assertEqual(self.source.debug_count({"available": True}), 1)
        self.assertEqual(self.source.debug_count({"available": False, "reason": "manual_only"}), 1)
        self.assertEqual(self.source.debug_count({"available": False, "reason": "no_data_found"}), 0)
        self.assertEqual(self.source.debug_count({}), 0)


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
