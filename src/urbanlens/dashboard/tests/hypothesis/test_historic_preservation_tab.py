"""P226: "NY Historic Preservation (CRIS)" and "Historic Registers" are one Property Records tab, "Historic Preservation".

Modelled on the Hudson River State Hospital campus, as P230's tests are: NPS lists the Main Building, its listing's
boundary holds other buildings, and CRIS's record of each building says which one is listed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from django.http import HttpResponse

_NY_ISH = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -73.0)])
_HOST = "redata_historic_registers"
_REFERENCE = "89001166"
_NPS_URL = f"https://npgallery.nps.gov/AssetDetail/NRIS/{_REFERENCE}"
_LISTING_NAME = "Hudson River State Hospital, Main Building"
_LISTED_NOTE = f"Listed on the National Register of Historic Places as “{_LISTING_NAME}”"

_CAMPUS = (Decimal("41.733016"), Decimal("-73.926380"))
_MAIN = (Decimal("41.733084"), Decimal("-73.928611"))
_STORAGE = (Decimal("41.732085"), Decimal("-73.926158"))

_NR_DISTRICT = {
    "NRNum": "94NR00622",
    "HistoricName": _LISTING_NAME,
    "EligibilityDesc": "Listed",
    "resource_type": "national_register_listing",
    "contains_point": True,
}


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


def _cris_record(name: str, eligibility: str, point: tuple[Decimal, Decimal]) -> dict:
    return {
        "USNName": name,
        "USNNum": "02714.000089",
        "EligibilityDesc": eligibility,
        "source_latitude": float(point[0]),
        "source_longitude": float(point[1]),
        "site_scope": False,
        "district": dict(_NR_DISTRICT),
    }


class _Campus(RedataConfiguredMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        patcher = mock.patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.campus = baker.make(
            Pin,
            profile=self.profile,
            location=baker.make(Location, latitude=_CAMPUS[0], longitude=_CAMPUS[1]),
            parent_pin=None,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=True,
            name="Hudson River State Hospital",
        )
        self.main = self._building("Kirkbride (Admin Building)", _MAIN)
        self.storage = self._building("BLDG 36/STORAGE (1942) - NON-CONTRIBUTING", _STORAGE)

    def _building(self, name: str, point: tuple[Decimal, Decimal]) -> Pin:
        return baker.make(
            Pin,
            profile=self.profile,
            location=baker.make(Location, latitude=point[0], longitude=point[1]),
            parent_pin=self.campus,
            pin_type=PinType.BUILDING,
            pin_type_is_user_provided=True,
            name=name,
        )

    def _tab(self, pin: Pin, *, registers: list[dict] | None = None, cris: dict | None = None) -> HttpResponse:
        if registers is not None:
            LocationCache.set(pin.location, _HOST, {"resources": registers}, query_key="")
        if cris is not None:
            LocationCache.set(pin.location, "cris_building_usn", cris, query_key="")
        with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as self.fetch_task:
            return self.client.get(reverse("pin.panel", args=[pin.slug, _HOST]))

    def _scheduled(self) -> set[str]:
        return {call.kwargs["args"][0] for call in self.fetch_task.apply_async.call_args_list}


class HistoricPreservationTabStripTests(_Campus):
    """One tab where there were two."""

    def _property_tabs(self) -> dict[str, str]:
        response = self.client.get(reverse("pin.details", args=[self.main.slug]))
        return {tab["key"]: tab["label"] for tab in response.context["property_tabs"]}

    def test_one_historic_preservation_tab(self) -> None:
        tabs = self._property_tabs()
        self.assertEqual(tabs.get(_HOST), "Historic Preservation")
        self.assertNotIn("cris_building", tabs)

    def test_neither_old_label_remains(self) -> None:
        content = self.client.get(reverse("pin.details", args=[self.main.slug])).content.decode()
        strip = content.split('id="property-records-section"')[1].split('id="property-records-body"')[0]
        self.assertNotIn("NY Historic Preservation (CRIS)", strip)
        self.assertNotIn(">Historic Registers<", strip)
        self.assertNotIn(reverse("pin.panel", args=[self.main.slug, "cris_building"]), content)


class HistoricPreservationTabTests(_Campus):
    """Both sources in one tab, each attributed, a fact they share given once."""

    def test_each_source_is_attributed(self) -> None:
        response = self._tab(
            self.campus, registers=[_listing()], cris={"site_scope": True, "district": dict(_NR_DISTRICT)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Historic Registers")
        self.assertContains(response, "NY Historic Preservation (CRIS)")
        self.assertContains(response, _LISTED_NOTE)
        self.assertContains(response, "94NR00622")

    def test_npss_number_is_given_once(self) -> None:
        """CRIS's card names NPS's number for its listing; the registers' row already does, linked."""
        response = self._tab(
            self.campus, registers=[_listing()], cris={"site_scope": True, "district": dict(_NR_DISTRICT)}
        )

        self.assertNotContains(response, "NRHP Reference Number")
        self.assertContains(response, f"#{_REFERENCE}")
        self.assertContains(response, f'href="{_NPS_URL}"')

    def test_the_listed_building_shows_its_listing(self) -> None:
        response = self._tab(
            self.main, registers=[_listing()], cris=_cris_record("BLDG 51/MAIN/ADMIN (1871) - NHL", "Listed", _MAIN)
        )

        self.assertContains(response, _LISTED_NOTE)
        self.assertContains(response, f'href="{_NPS_URL}"')
        self.assertContains(response, "BLDG 51/MAIN/ADMIN (1871) - NHL")

    def test_a_building_inside_the_listings_boundary_is_not_listed_by_it(self) -> None:
        """P230's rule holds in the merged tab: CRIS calls this building eligible, not listed."""
        response = self._tab(
            self.storage,
            registers=[_listing()],
            cris=_cris_record("BLDG 36/STORAGE (1942) - NON-CONTRIBUTING", "Eligible", _STORAGE),
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, _LISTED_NOTE)
        self.assertNotContains(response, _NPS_URL)
        self.assertContains(response, "Eligible")

    def test_a_source_still_fetching_is_fetched_while_the_other_shows(self) -> None:
        response = self._tab(self.campus, cris={"site_scope": True, "district": dict(_NR_DISTRICT)})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "94NR00622")
        self.assertEqual(self._scheduled(), {_HOST})
        self.assertContains(response, "?attempt=1")

    def test_with_neither_fetched_both_are_fetched_behind_a_visible_spinner(self) -> None:
        response = self._tab(self.campus)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._scheduled(), {_HOST, "cris_building"})
        self.assertContains(response, "view-loading")
        self.assertNotIn(" hidden", response.content.decode().strip().split(">", 1)[0])

    def test_with_nothing_from_either_there_is_no_tab(self) -> None:
        response = self._tab(self.campus, registers=[], cris={})

        self.assertEqual(response.status_code, 204)

    def test_outside_new_york_only_the_registers_are_asked(self) -> None:
        location = baker.make(Location, latitude=Decimal("34.052200"), longitude=Decimal("-118.243700"))
        pin = baker.make(Pin, profile=self.profile, location=location, parent_pin=None)

        self._tab(pin)

        self.assertEqual(self._scheduled(), {_HOST})


class HistoricPreservationBuildingCardTests(_Campus):
    """A building's card on its campus page asks for the merged panel once, not each source."""

    def test_the_card_requests_one_historic_preservation_panel(self) -> None:
        LocationCache.set(self.campus.location, PARCEL_BUILDINGS_CACHE_SOURCE, {})
        content = self.client.get(reverse("pin.child_building", args=[self.main.slug])).content.decode()

        self.assertEqual(content.count(reverse("pin.building_panel", args=[self.main.slug, _HOST])), 1)
        self.assertNotIn(reverse("pin.building_panel", args=[self.main.slug, "cris_building"]), content)

    def test_the_card_shows_both_sources(self) -> None:
        LocationCache.set(self.main.location, _HOST, {"resources": [_listing()]}, query_key="")
        LocationCache.set(
            self.main.location, "cris_building_usn", _cris_record("BLDG 51/MAIN/ADMIN (1871) - NHL", "Listed", _MAIN)
        )

        response = self.client.get(reverse("pin.building_panel", args=[self.main.slug, _HOST]))

        self.assertContains(response, _LISTED_NOTE)
        self.assertContains(response, "BLDG 51/MAIN/ADMIN (1871) - NHL")
        self.assertContains(response, f'id="historic-registers-section--{self.main.slug}"')
        self.assertContains(response, "Historic Preservation")
