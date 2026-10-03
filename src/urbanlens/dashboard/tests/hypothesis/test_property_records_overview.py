"""P226: Property Records' Overview summarises the card's other tabs, and a tab still fetching is never blank."""

from __future__ import annotations

from decimal import Decimal
import json
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from django.http import HttpResponse

#: Covers upstate New York, so CRIS's gate needs no TIGERweb call.
_NY_ISH = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -73.0)])

_OWNER = "EFG/DRA Heritage LLC"
_LOCKED_CHIP = "Owner on record - subscribers only"
_REFERENCE = "89001166"
_NPS_URL = f"https://npgallery.nps.gov/AssetDetail/NRIS/{_REFERENCE}"
_LISTING_NAME = "Hudson River State Hospital, Main Building"
_HRSH = (Decimal("41.733100"), Decimal("-73.928600"))

_PARCEL = {
    "available": True,
    "apn": "6163-03-011149-0000",
    "owner_name": [_OWNER],
    "year_built": 1871,
    "situs_address": "3532 North Rd",
    "market_value": 4594300.0,
}

_PROPERTY_KEYS = ("property_records", "overture_building_attributes", "redata_historic_registers", "cris_building")


def _listing(**overrides) -> dict:
    return {
        "provider": "nps_nrhp",
        "resource_type": "building",
        "scope": "structure",
        "external_id": _REFERENCE,
        "name": _LISTING_NAME,
        "status": "Listed",
        "contains_point": True,
        "source_latitude": None,
        "source_longitude": None,
        **overrides,
    }


class _Base(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = baker.make(Location, latitude=_HRSH[0], longitude=_HRSH[1])
        self.pin: Pin = baker.make(Pin, profile=self.user.profile, location=self.location, parent_pin=None)
        patcher = mock.patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _subscribe(self) -> None:
        role = baker.make(SubscriptionRole, features=SiteFeature.PROPERTY_OWNERS)
        grant_subscription(self.user, role, self.user, None)

    def _settle(self, **payloads: dict) -> None:
        """Cache an answer for every Property Records source, ``{}`` unless given."""
        sources = {
            "property_records": "property_records",
            "overture_building_attributes": "overture_building_attributes",
        }
        sources |= {"redata_historic_registers": "redata_historic_registers", "cris_building": "cris_building_usn"}
        for key, cache_source in sources.items():
            LocationCache.set(self.location, cache_source, payloads.get(key, {}), query_key="")

    def _overview(self, pin: Pin | None = None) -> tuple[HttpResponse, set[str]]:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            response = self.client.get(reverse("pin.property_records_overview", args=[(pin or self.pin).slug]))
        return response, {call.kwargs["args"][0] for call in fetch_task.apply_async.call_args_list}

    @staticmethod
    def _hidden(response: HttpResponse) -> list[str]:
        if "HX-Trigger" not in response:
            return []
        return json.loads(response["HX-Trigger"])["pinTabsEmpty"]["keys"]


class PropertyRecordsCardTests(_Base):
    """The card opens on the Overview; the parcel's own record is a tab of its own."""

    def _strip(self) -> str:
        content = self.client.get(reverse("pin.details", args=[self.pin.slug])).content.decode()
        return content.split('id="property-records-section"')[1].split('id="property-records-body"')[0]

    def test_the_overview_tab_loads_the_summary(self) -> None:
        strip = self._strip()
        overview = strip.split("<button")[1]
        self.assertIn(reverse("pin.property_records_overview", args=[self.pin.slug]), overview)
        self.assertIn("ul:lazy-load", overview)
        self.assertIn(">Overview<", overview)

    def test_the_parcel_record_is_its_own_tab(self) -> None:
        strip = self._strip()
        self.assertIn(f'hx-get="{reverse("pin.panel", args=[self.pin.slug, "property_records"])}"', strip)
        self.assertIn('data-panel-key="property_records"', strip)
        self.assertIn(">Parcel<", strip)


class PropertyRecordsOverviewTests(_Base):
    """Owner, parcel, year built, historic status and register number, from whichever tab has them."""

    def test_a_subscriber_sees_the_owner(self) -> None:
        self._subscribe()
        self._settle(property_records=_PARCEL)

        response, _scheduled = self._overview()

        self.assertContains(response, _OWNER)
        self.assertNotContains(response, _LOCKED_CHIP)

    def test_anyone_else_is_told_an_owner_is_on_record_but_not_who(self) -> None:
        """The Parcel tab's gate (``can_see_official_owners``) applies here too."""
        self._settle(property_records=_PARCEL)

        response, _scheduled = self._overview()

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _OWNER)
        self.assertNotContains(response, "EFG")
        self.assertContains(response, _LOCKED_CHIP)

    def test_the_parcel_and_year_built(self) -> None:
        self._settle(property_records=_PARCEL)

        response, _scheduled = self._overview()

        self.assertContains(response, "6163-03-011149-0000")
        self.assertContains(response, "1871")

    def test_the_parcel_tabs_detail_stays_on_its_tab(self) -> None:
        self._settle(property_records=_PARCEL)

        response, _scheduled = self._overview()

        self.assertNotContains(response, "4,594,300")

    def test_the_national_register_listing_and_its_number(self) -> None:
        self._settle(redata_historic_registers={"resources": [_listing()]})

        response, _scheduled = self._overview()

        self.assertContains(response, f"Listed on the National Register of Historic Places as “{_LISTING_NAME}”")
        self.assertContains(response, f'href="{_NPS_URL}"')
        self.assertContains(response, f">{_REFERENCE}<")

    def test_a_neighbours_listing_is_not_this_places_status(self) -> None:
        self._settle(
            redata_historic_registers={
                "resources": [_listing(contains_point=False, name="Roosevelt, Isaac, House", external_id="72000839")]
            }
        )

        response, _scheduled = self._overview()

        self.assertNotContains(response, "Roosevelt", status_code=response.status_code)
        self.assertNotContains(response, "72000839", status_code=response.status_code)

    def test_crises_eligibility_for_the_building_it_records(self) -> None:
        record = {"USNName": "BLDG 51/MAIN/ADMIN (1871) - NHL", "EligibilityDesc": "Eligible"}
        record |= {"source_latitude": float(_HRSH[0]), "source_longitude": float(_HRSH[1])}
        self._settle(cris_building=record)

        response, _scheduled = self._overview()

        self.assertContains(response, "Eligible")

    def test_crises_record_of_another_building_is_not_this_ones(self) -> None:
        """CRIS's nearest record within 200 m can be a neighbour's (P255)."""
        record = {"USNName": "BLDG 166/OLD POLICE STATION (1932)", "EligibilityDesc": "Not Eligible"}
        record |= {"source_latitude": float(_HRSH[0]) + 0.001, "source_longitude": float(_HRSH[1])}
        self._settle(cris_building=record)

        response, _scheduled = self._overview()

        self.assertNotContains(response, "Not Eligible", status_code=response.status_code)

    def test_a_fact_two_tabs_share_is_given_once(self) -> None:
        """CRIS's site record and the registers both name NPS's number for the same listing."""
        self.pin.pin_type, self.pin.pin_type_is_user_provided = PinType.PARCEL, True
        self.pin.save(update_fields=["pin_type", "pin_type_is_user_provided"])
        district = {"HistoricName": _LISTING_NAME, "NRNum": "94NR00622", "EligibilityDesc": "Listed"}
        district |= {"resource_type": "national_register_listing", "contains_point": True}
        self._settle(cris_building={"district": district}, redata_historic_registers={"resources": [_listing()]})

        response, _scheduled = self._overview()

        self.assertEqual(response.content.decode().count(f">{_REFERENCE}<"), 1)

    def test_nothing_known_is_no_overview(self) -> None:
        self._settle()

        response, scheduled = self._overview()

        self.assertEqual(response.status_code, 204)
        self.assertEqual(scheduled, set())


class PropertyRecordsOverviewFetchTests(_Base):
    """The Overview fetches what its tabs need, and names the tabs that turned out empty."""

    def test_unfetched_tabs_are_fetched_and_polled_for(self) -> None:
        response, scheduled = self._overview()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scheduled, set(_PROPERTY_KEYS))
        self.assertContains(response, "?attempt=1")

    def test_the_placeholder_is_visible(self) -> None:
        """A placeholder in a tab body is the tab's only content; hidden, the tab reads as blank."""
        response, _scheduled = self._overview()

        self.assertContains(response, "view-loading")
        self.assertNotIn(" hidden", response.content.decode().split(">", 1)[0])

    def test_a_tab_with_nothing_is_named_so_it_can_be_removed(self) -> None:
        self._settle(property_records=_PARCEL)

        response, _scheduled = self._overview()

        self.assertEqual(set(self._hidden(response)), {"overture_building_attributes", "redata_historic_registers"})

    def test_historic_preservation_stays_while_either_source_has_something(self) -> None:
        record = {"USNName": "BLDG 51/MAIN/ADMIN (1871) - NHL", "EligibilityDesc": "Eligible"}
        record |= {"source_latitude": float(_HRSH[0]), "source_longitude": float(_HRSH[1])}
        self._settle(property_records=_PARCEL, cris_building=record)

        response, _scheduled = self._overview()

        self.assertNotIn("redata_historic_registers", self._hidden(response))

    def test_a_source_outside_its_region_is_not_fetched(self) -> None:
        """CRIS is New York's: a Californian pin never asks it."""
        location = baker.make(Location, latitude=Decimal("34.052200"), longitude=Decimal("-118.243700"))
        pin = baker.make(Pin, profile=self.user.profile, location=location, parent_pin=None)

        _response, scheduled = self._overview(pin)

        self.assertNotIn("cris_building", scheduled)
        self.assertIn("redata_historic_registers", scheduled)

    def test_another_users_pin_is_not_found(self) -> None:
        other: Pin = baker.make_recipe("dashboard.pin")
        response, scheduled = self._overview(other)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(scheduled, set())

    def test_the_query_count_does_not_grow_with_the_records(self) -> None:
        self._subscribe()

        def queries(rows: int) -> int:
            resources = [_listing(name=f"Listing {index}", external_id=f"8900{index:04d}") for index in range(rows)]
            self._settle(property_records=_PARCEL, redata_historic_registers={"resources": resources})
            with CaptureQueriesContext(connection) as captured:
                self._overview()
            return len(captured.captured_queries)

        queries(1)  # warms the per-process caches the first request fills
        self.assertEqual(queries(1), queries(12))


class LocationDataOverviewLeavesPropertyRecordsAloneTests(_Base):
    """Each card's Overview looks after its own tabs."""

    def test_location_datas_overview_no_longer_fetches_property_records_tabs(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
            self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        scheduled = {call.kwargs["args"][0] for call in fetch_task.apply_async.call_args_list}

        self.assertEqual(scheduled & set(_PROPERTY_KEYS), set())


class TabPendingPlaceholderTests(_Base):
    """A tab whose data is still being fetched shows a spinner; a standalone card stays hidden until it has content."""

    def _pending(self, key: str) -> str:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.panel", args=[self.pin.slug, key]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    @staticmethod
    def _opening_tag(content: str) -> str:
        return content.strip().split(">", 1)[0]

    def test_a_pending_tab_is_visible(self) -> None:
        content = self._pending("property_records")
        self.assertIn("view-loading", content)
        self.assertNotIn(" hidden", self._opening_tag(content))

    def test_a_pending_tab_still_reports_its_204(self) -> None:
        """The fallback handler turns the placeholder's last, empty poll into "No data available."."""
        self.assertIn("data-ext-panel-204", self._opening_tag(self._pending("property_records")))

    def test_a_pending_standalone_card_starts_hidden(self) -> None:
        self.assertIn(" hidden", self._opening_tag(self._pending("redata_underground")))

    def test_location_datas_pending_overview_is_visible(self) -> None:
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source"):
            response = self.client.get(reverse("pin.location_data_overview", args=[self.pin.slug]))
        self.assertNotIn(" hidden", self._opening_tag(response.content.decode()))
