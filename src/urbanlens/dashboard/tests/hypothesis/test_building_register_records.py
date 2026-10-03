"""P230: register details appear on the buildings the register lists, and every building child links its wiki.

Modelled on the Hudson River State Hospital campus: NPS lists one building there (the Main Building, NHL), and its
listing's boundary holds a dozen others. CRIS's own record for each building says which one is listed.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.redata_historic_registers import HistoricRegisterPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE

_LISTING_NAME = "Hudson River State Hospital, Main Building"
_NPS_URL = "https://npgallery.nps.gov/AssetDetail/NRIS/89001166"
_LISTED_NOTE = f"Listed on the National Register of Historic Places as “{_LISTING_NAME}”"

_CAMPUS = ("41.733016", "-73.926380")
_MAIN = ("41.733084", "-73.928611")
_STORAGE = ("41.732085", "-73.926158")
#: 120 m north of the Main Building, and further than that from everything else here.
_CHAPEL = ("41.734162", "-73.928611")
_LAUNDRY = ("41.733800", "-73.927200")

#: CRIS's National Register listing record, which reached every building inside its boundary.
_NR_DISTRICT = {
    "NRNum": "94NR00622",
    "HistoricName": _LISTING_NAME,
    "resource_type": "national_register_listing",
    "contains_point": True,
}


def _listing(**overrides) -> dict:
    """NPS's own row: a building listing with a boundary but no point of its own."""
    return {
        "provider": "nps_nrhp",
        "resource_type": "building",
        "scope": "structure",
        "external_id": "89001166",
        "name": _LISTING_NAME,
        "status": "Listed",
        "contains_point": True,
        "source_latitude": None,
        "source_longitude": None,
        **overrides,
    }


def _cris_record(name: str, eligibility: str, point: tuple[str, str]) -> dict:
    return {
        "USNName": name,
        "USNNum": "02714.000000",
        "EligibilityDesc": eligibility,
        "source_latitude": float(point[0]),
        "source_longitude": float(point[1]),
        "site_scope": False,
        "district": dict(_NR_DISTRICT),
    }


class _Campus(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.source = HistoricRegisterPanelSource()
        self.campus = baker.make(
            Pin,
            profile=self.profile,
            location=self._location(_CAMPUS),
            parent_pin=None,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=True,
            name="Hudson River State Hospital",
        )
        self.main = self._building("Kirkbride (Admin Building)", _MAIN)
        self.storage = self._building("BLDG 36/STORAGE (1942) - NON-CONTRIBUTING", _STORAGE)
        LocationCache.set(
            self.main.location, "cris_building_usn", _cris_record("BLDG 51/MAIN/ADMIN (1871) - NHL", "Listed", _MAIN)
        )
        LocationCache.set(
            self.storage.location,
            "cris_building_usn",
            _cris_record("BLDG 36/STORAGE (1942) - NON-CONTRIBUTING", "Eligible", _STORAGE),
        )

    @staticmethod
    def _location(point: tuple[str, str]) -> Location:
        return baker.make(Location, latitude=point[0], longitude=point[1])

    def _building(self, name: str, point: tuple[str, str], **kwargs) -> Pin:
        return baker.make(
            Pin,
            profile=self.profile,
            location=self._location(point),
            parent_pin=self.campus,
            pin_type=kwargs.pop("pin_type", PinType.BUILDING),
            pin_type_is_user_provided=True,
            name=name,
            **kwargs,
        )

    def _render(self, pin: Pin, *rows: dict) -> dict | None:
        return self.source.render_context(pin, {"resources": list(rows)})


class BuildingRegisterRecordTests(_Campus):
    """A building shows a listing only when the register's record is that building's own."""

    def test_a_building_inside_the_listings_boundary_is_not_listed_by_it(self) -> None:
        self.assertIsNone(self._render(self.storage, _listing()))

    def test_the_building_cris_records_as_listed_shows_the_listing(self) -> None:
        context = self._render(self.main, _listing())

        assert context is not None
        self.assertEqual(context["facts"][0]["text"], _LISTED_NOTE)
        self.assertIn(_LISTING_NAME, context["meta"][0]["value"])

    def test_the_cris_record_must_stand_on_the_building(self) -> None:
        """CRIS's nearest record within 200 m can be another building's: this one is the Main Building's."""
        chapel = self._building("BLDG 35/PROTESTANT CHAPEL (1925)", _CHAPEL)
        LocationCache.set(
            chapel.location, "cris_building_usn", _cris_record("BLDG 51/MAIN/ADMIN (1871) - NHL", "Listed", _MAIN)
        )

        self.assertIsNone(self._render(chapel, _listing()))

    def test_crises_listing_record_alone_does_not_list_a_building(self) -> None:
        """The note fell back to CRIS's site record whenever no register row held the point."""
        neighbour = _listing(name="Roosevelt, Isaac, House", external_id="72000839", contains_point=False)

        context = self._render(self.storage, neighbour)

        self.assertNotIn(_LISTING_NAME, str(context))

    def test_a_listing_whose_own_point_is_on_a_building_belongs_to_that_building(self) -> None:
        laundry = self._building("BLDG 50/LAUNDRY & TAILOR SHOP (1871)", _LAUNDRY)
        own = _listing(
            name="Laundry",
            external_id="12345678",
            contains_point=False,
            source_latitude=float(_LAUNDRY[0]),
            source_longitude=float(_LAUNDRY[1]),
        )

        self.assertIsNotNone(self._render(laundry, own))
        self.assertIsNone(self._render(self.storage, own))

    def test_a_districts_point_landing_on_a_building_does_not_make_it_the_districts(self) -> None:
        district = _listing(
            name="Hudson River State Hospital Historic District",
            resource_type="building_district",
            scope="site",
            contains_point=False,
            source_latitude=float(_STORAGE[0]),
            source_longitude=float(_STORAGE[1]),
        )

        self.assertIsNone(self._render(self.storage, district))

    def test_the_campus_shows_its_listing(self) -> None:
        context = self._render(self.campus, _listing())

        assert context is not None
        self.assertEqual(context["facts"][0]["text"], _LISTED_NOTE)

    def test_a_pin_on_its_own_inside_the_boundary_keeps_the_listing(self) -> None:
        """Not one building of a larger site: the listing's boundary holding it is what there is to go on."""
        standalone = baker.make(
            Pin,
            profile=self.profile,
            location=self._location(("41.732095", "-73.926158")),
            parent_pin=None,
            pin_type=PinType.BUILDING,
        )

        context = self._render(standalone, _listing())

        assert context is not None
        self.assertEqual(context["facts"][0]["text"], _LISTED_NOTE)


class BuildingRegisterLinkTests(_Campus):
    """The automatic link follows the same rule: the listed building's pin and wiki get it, its neighbours' do not."""

    def setUp(self) -> None:
        super().setUp()
        self.main_wiki = baker.make(Wiki, location=self.main.location, name="Kirkbride")
        self.storage_wiki = baker.make(Wiki, location=self.storage.location, name="Storage")

    def _fetch(self, pin: Pin) -> None:
        envelope = LocationContextEnvelope(count=1, complete=True, results=[_listing()])
        with mock.patch.object(HistoricRegisterPanelSource, "fetch_envelope", return_value=envelope):
            self.source.fetch(pin)

    def test_the_listed_building_gets_the_link(self) -> None:
        self._fetch(self.main)

        self.assertTrue(PinLink.objects.filter(pin=self.main, url=_NPS_URL).exists())
        self.assertTrue(WikiLink.objects.filter(wiki=self.main_wiki, url=_NPS_URL).exists())

    def test_crises_record_landing_after_the_registers_links_the_listing(self) -> None:
        """The card fetches both at once; the registers can settle first, before CRIS says the building is listed."""
        from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource
        from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

        LocationCache.objects.filter(location=self.main.location, source="cris_building_usn").delete()
        self._fetch(self.main)
        self.assertFalse(PinLink.objects.filter(pin=self.main).exists())

        resource = {
            "uuid": "res-main",
            "provider": "ny_cris",
            "resource_type": "building",
            "name": "BLDG 51/MAIN/ADMIN (1871) - NHL",
            "source_latitude": float(_MAIN[0]),
            "source_longitude": float(_MAIN[1]),
            "attributes": {"USNName": "BLDG 51/MAIN/ADMIN (1871) - NHL", "EligibilityDesc": "Listed"},
            "attachments": [],
        }
        with (
            mock.patch.object(RedataGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataGateway, "lookup_cultural_resources", return_value=[resource]),
            mock.patch.object(RedataGateway, "fetch_cultural_resource_detail", return_value=resource),
        ):
            CrisBuildingPanelSource().fetch(self.main)

        self.assertTrue(PinLink.objects.filter(pin=self.main, url=_NPS_URL).exists())

    def test_a_building_sharing_its_grounds_does_not(self) -> None:
        self._fetch(self.storage)

        self.assertFalse(PinLink.objects.filter(pin=self.storage).exists())
        self.assertFalse(WikiLink.objects.filter(wiki=self.storage_wiki).exists())


class BuildingsListTests(_Campus):
    """Every building a child pin covers opens its card in place, whatever type the child is stored as."""

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.user)
        self.laundry = self._building("Laundry", _LAUNDRY, pin_type=PinType.LOCATION_MARKER)
        self.laundry_wiki = baker.make(Wiki, location=self.laundry.location, name="Laundry & Tailor Shop")
        buildings = [
            {
                "name": "BLDG 50/LAUNDRY & TAILOR SHOP (1871)",
                "latitude": float(_LAUNDRY[0]),
                "longitude": float(_LAUNDRY[1]),
                "ref": "cris:02714.000108",
                "source": "cris",
            },
            {
                "name": "BLDG 51/MAIN/ADMIN (1871) - NHL",
                "latitude": float(_MAIN[0]),
                "longitude": float(_MAIN[1]),
                "ref": "cris:02714.000089",
                "source": "cris",
            },
        ]
        LocationCache.set(
            self.campus.location, PARCEL_BUILDINGS_CACHE_SOURCE, {"buildings": buildings, "provider": "cris"}
        )

    def _list(self) -> str:
        with mock.patch("urbanlens.dashboard.services.pins.auto_nest.request_sweep"):
            response = self.client.get(reverse("pin.parcel_buildings", args=[self.campus.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_building_covered_by_a_child_of_another_type_opens_in_place(self) -> None:
        content = self._list()

        self.assertIn(f'hx-get="{reverse("pin.child_building", args=[self.laundry.slug])}"', content)

    def test_a_building_child_still_opens_in_place(self) -> None:
        content = self._list()

        self.assertIn(f'hx-get="{reverse("pin.child_building", args=[self.main.slug])}"', content)

    def test_the_opened_card_links_the_childs_wiki(self) -> None:
        response = self.client.get(reverse("pin.child_building", args=[self.laundry.slug]))

        self.assertContains(response, reverse("location.wiki", kwargs={"location_slug": self.laundry.location.slug}))
