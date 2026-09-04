"""Tests for round 3 of the REData integration (2026-08-15).

Fire & disaster history panel, the aerial media gallery source, assessment
history on the property card, and the Chronicling America provider's flags.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.plugins.builtin.hazard_history import HazardHistoryPanelSource
from urbanlens.dashboard.plugins.builtin.property_records import (
    _assessment_history,
    _render_available,
    _supplementary_sales,
)
from urbanlens.dashboard.plugins.builtin.redata_aerial_media import AerialMediaSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextEnvelope,
    LocationContextUnavailableError,
)


class AssessmentHistoryTests(SimpleTestCase):
    def test_matches_the_records_own_apn_despite_formatting(self) -> None:
        """Assessors and GIS vendors format the same PIN differently."""
        rows = [
            {"parcel_identifier": "16-07-114-011-0000", "tax_year": 2024, "total_value": 45000, "value_stage": "board"},
            {"parcel_identifier": "1607114012", "tax_year": 2024, "total_value": 99000, "value_stage": "mailed"},
        ]
        history = _assessment_history(rows, "1607-114-011-0000")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["total_value"], 45000)

    def test_a_known_apn_with_no_rows_shows_nothing_not_a_neighbour(self) -> None:
        """Falling back when OUR parcel is uncovered would display someone else's valuations."""
        rows = [{"parcel_identifier": "neighbour-pin", "tax_year": 2024, "total_value": 999}]
        self.assertEqual(_assessment_history(rows, "our-own-pin"), [])

    def test_without_an_apn_takes_the_identifier_with_the_most_rows(self) -> None:
        rows = [
            {"parcel_identifier": "A", "tax_year": 2024, "total_value": 1},
            {"parcel_identifier": "B", "tax_year": 2024, "total_value": 2},
            {"parcel_identifier": "B", "tax_year": 2023, "total_value": 3},
        ]
        history = _assessment_history(rows, "")
        self.assertEqual([row["total_value"] for row in history], [2, 3])

    def test_orders_newest_first_and_drops_valueless_rows(self) -> None:
        rows = [
            {"parcel_identifier": "A", "tax_year": 2022, "total_value": 10},
            {"parcel_identifier": "A", "tax_year": 2024, "total_value": None},
            {"parcel_identifier": "A", "tax_year": 2023, "total_value": 30},
        ]
        history = _assessment_history(rows, "A")
        self.assertEqual([row["tax_year"] for row in history], [2023, 2022])

    def test_history_rows_render_with_year_and_stage(self) -> None:
        data = {
            "available": True,
            "situs_address": "1 Main St",
            "assessment_history": [{"tax_year": 2024, "total_value": 45000, "value_stage": "board"}],
        }
        context = _render_available(data, show_owner=False)
        values = {entry["label"]: entry["value"] for entry in context["meta"]}
        self.assertEqual(values.get("Assessed 2024"), "$45,000 (board)")


class SupplementarySalesTests(SimpleTestCase):
    def test_matches_by_normalized_situs_address(self) -> None:
        rows = [
            {"situs_address": "323 BEAVER ST.", "sale_date": "2021-04-14", "sale_price": "248400.00"},
            {"situs_address": "325 Beaver St", "sale_date": "2020-01-01", "sale_price": "99000.00"},
        ]
        sales = _supplementary_sales(rows, "323 Beaver St", "")
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0]["date"], "2021-04-14")

    def test_matches_by_raw_pin_when_the_address_differs(self) -> None:
        rows = [
            {
                "situs_address": "UNIT 4B",
                "sale_date": "2019-06-01",
                "sale_price": "150000",
                "attributes": {"pin": "16-07-114-011-0000"},
            }
        ]
        sales = _supplementary_sales(rows, "323 Beaver St", "1607114011 0000")
        self.assertEqual(len(sales), 1)

    def test_unmatched_rows_are_dropped_not_attributed(self) -> None:
        """A near-parcel row that matches neither address nor PIN is a neighbour's sale."""
        rows = [{"situs_address": "999 Other Ave", "sale_date": "2021-04-14", "sale_price": "1000000"}]
        self.assertEqual(_supplementary_sales(rows, "323 Beaver St", "our-pin"), [])

    def test_non_arms_length_rows_are_excluded(self) -> None:
        """A $1 trust conveyance or a bundle sale is a real transfer but not this parcel's market price."""
        rows = [
            {
                "situs_address": "323 Beaver St",
                "sale_date": "2021-04-14",
                "sale_price": "1",
                "attributes": {"arms_length": False},
            },
            {
                "situs_address": "323 Beaver St",
                "sale_date": "2018-02-02",
                "sale_price": "200000",
                "attributes": {"arms_length": True},
            },
            {"situs_address": "323 Beaver St", "sale_date": "2015-03-03", "sale_price": "180000"},
        ]
        sales = _supplementary_sales(rows, "323 Beaver St", "")
        self.assertEqual([sale["date"] for sale in sales], ["2015-03-03", "2018-02-02"])

    def test_rows_without_date_or_price_are_dropped_and_output_is_pipeline_shaped(self) -> None:
        rows = [
            {"situs_address": "323 Beaver St", "sale_date": None, "sale_price": None},
            {"situs_address": "323 Beaver St", "sale_date": "2021-04-14", "sale_price": "248400.00"},
        ]
        sales = _supplementary_sales(rows, "323 Beaver St", "")
        self.assertEqual(sales, [{"date": "2021-04-14", "price": "248400.00", "grantor": "", "grantee": ""}])


class HazardHistoryPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = HazardHistoryPanelSource()
        location = baker.make("dashboard.Location", latitude=41.9, longitude=-87.7)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_fetch_keeps_only_fire_and_fema_providers(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=3,
            complete=True,
            results=[
                {"provider": "usgs_earthquakes", "event_type": "earthquake"},
                {"provider": "nifc_wildfires", "event_type": "wildfire", "occurred_at": "1988-01-01"},
                {"provider": "fema_disasters", "event_type": "flood", "occurred_at": "2011-04-22"},
            ],
        )
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_hazards_gateway.RedataHazardsGateway"
        ) as gateway_cls:
            gateway_cls.return_value.get_hazard_events.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "hazard_history")
        assert cached is not None
        providers = {event["provider"] for event in cached.data["events"]}
        self.assertEqual(providers, {"nifc_wildfires", "fema_disasters"})
        # ?provider= restricts which sources RUN - the earthquake catalog must
        # not be fetched just to be discarded.
        self.assertEqual(
            gateway_cls.return_value.get_hazard_events.call_args.kwargs.get("providers"),
            ["nifc_wildfires", "fema_disasters"],
        )

    def test_render_names_programs_and_sizes(self) -> None:
        data = {
            "events": [
                {
                    "provider": "nifc_wildfires",
                    "event_type": "wildfire",
                    "occurred_at": "1988-01-01",
                    "title": "Canyon Fire",
                    "magnitude": 12500.0,
                    "magnitude_scale": "acres_burned",
                },
                {
                    "provider": "fema_disasters",
                    "event_type": "flood",
                    "occurred_at": "2011-04-22",
                    "attributes": {"designated_area": "Cook County", "programs": ["public assistance"]},
                },
            ],
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("1 wildfire reached within 2 km", ctx["chips"])
        self.assertIn("1 federal disaster declaration for this county", ctx["chips"])
        values = " | ".join(entry["value"] for entry in ctx["meta"])
        self.assertIn("12,500 acres", values)
        self.assertIn("public assistance", values)

    def test_empty_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"events": []}))


class AerialMediaSourceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = AerialMediaSource()
        location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
        self.pin = baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)

    def test_fetch_requests_aerial_only_and_caches(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_media_gateway.RedataMediaGateway"
        ) as gateway_cls:
            gateway_cls.return_value.lookup.return_value = [
                {"url": "https://example.test/a.mp4", "title": "Drone flyover"}
            ]
            self.source.fetch(self.pin)

        gateway_cls.return_value.lookup.assert_called_once_with(40.5, -74.5, is_aerial=True, limit=24)
        cached = LocationCache.get_fresh(self.pin.location, "redata_aerial")
        assert cached is not None
        self.assertEqual(len(cached.data["items"]), 1)

    def test_media_items_maps_rows_and_skips_urlless(self) -> None:
        data = {
            "items": [
                {"url": "https://example.test/a.jpg", "title": "Roof view", "credit": "Wikimedia Commons"},
                {"title": "no url"},
            ]
        }
        items = self.source.media_items(data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].caption, "Roof view")
        self.assertEqual(items[0].source, "Wikimedia Commons")


class RedataCapabilitiesHelperTests(TestCase):
    def tearDown(self) -> None:
        from django.core.cache import cache

        cache.clear()
        super().tearDown()

    def test_unconfigured_redata_yields_none_without_a_request(self) -> None:
        from urbanlens.dashboard.controllers.site_admin import _redata_capabilities

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured", return_value=False
        ):
            self.assertIsNone(_redata_capabilities())

    def test_the_index_is_cached_for_subsequent_loads(self) -> None:
        from urbanlens.dashboard.controllers.site_admin import _redata_capabilities

        body = {"domains": [{"tag": "weather", "endpoint": "/api/v1/weather/", "providers": []}], "text_domains": []}
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=True,
            ),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway.RedataCapabilitiesGateway"
            ) as gateway_cls,
        ):
            gateway_cls.return_value.get_capabilities.return_value = body
            first = _redata_capabilities()
            second = _redata_capabilities()

        self.assertEqual(first, body)
        self.assertEqual(second, body)
        gateway_cls.return_value.get_capabilities.assert_called_once()

    def test_an_outage_yields_none_and_does_not_retry_every_load(self) -> None:
        from urbanlens.dashboard.controllers.site_admin import _redata_capabilities

        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=True,
            ),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_capabilities_gateway.RedataCapabilitiesGateway"
            ) as gateway_cls,
        ):
            gateway_cls.return_value.get_capabilities.side_effect = LocationContextUnavailableError(
                "source_error", "down"
            )
            first = _redata_capabilities()
            second = _redata_capabilities()

        self.assertIsNone(first)
        self.assertIsNone(second)
        gateway_cls.return_value.get_capabilities.assert_called_once()


class ChroniclingAmericaProviderTests(SimpleTestCase):
    def test_flags_match_the_loc_family(self) -> None:
        """Same LOC search infrastructure as library_of_congress; a modern street address is never signal for 1794-1963 newspaper text."""
        from urbanlens.dashboard.services.apis.locations.redata_reference_documents_gateway import (
            ChroniclingAmericaMediaProvider,
        )

        self.assertEqual(ChroniclingAmericaMediaProvider._redata_provider, "chronicling_america")
        self.assertFalse(ChroniclingAmericaMediaProvider.include_address)
        self.assertTrue(ChroniclingAmericaMediaProvider.reject_address_derived_names)
