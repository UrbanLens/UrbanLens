"""A building's Location takes its own provider name, never the name of the campus it stands in (P231).

On production (v0.8.0) the wiki of HRSH's "BLDG 33/POWERHOUSE & MACHINE SHOP (1929)" child pin was titled "Hudson
River State Hospital" and reached by the Location's uuid. On the dev stack, ten building Locations under the campus
were named "Hudson River State Hospital" from Wikipedia (the campus article the Wikipedia panel falls back to on a
child pin), with slugs minted from it, and most of the rest had no provider name at all though their CRIS card
named the building: a CRIS card refreshed the Location's names only when a register listing held the point.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.cache import signals as cache_signals
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.registry import plugin_registry
from urbanlens.dashboard.services.core.slugs import is_uuid_slug
from urbanlens.dashboard.services.locations import name_tiers, national_register
from urbanlens.dashboard.services.locations.name_resolution import NameProvider
from urbanlens.dashboard.services.locations.name_tiers import NamingScope, building_name_admissible
from urbanlens.dashboard.services.locations.naming import (
    external_name_candidates_for_location,
    update_location_name_from_external_sources,
)
from urbanlens.dashboard.services.locations.register_names import register_listing_names

_CAMPUS = "Hudson River State Hospital"
_LISTING = "Hudson River State Hospital, Main Building"
_BLDG33 = "BLDG 33/POWERHOUSE & MACHINE SHOP (1929)"
_LAT, _LNG = 41.73460, -73.92640
_OWN_POINT = (41.73461, -73.92641)
_NEIGHBOUR_POINT = (41.73500, -73.92700)


class _Provider(NameProvider):
    def __init__(self, source: str, *names: str) -> None:
        super().__init__(source=source)
        self._names = list(names)

    def candidates(self, location: Location) -> list[str | None]:
        return list(self._names)


def _row(data: dict) -> SimpleNamespace:
    return SimpleNamespace(data=data)


def _cris(
    *, point: tuple[float, float] | None = _OWN_POINT, eligibility: str = "Eligible", district: dict | None = None
) -> dict:
    data: dict = {"USNName": _BLDG33, "EligibilityDesc": eligibility}
    if point is not None:
        data.update(source_latitude=point[0], source_longitude=point[1])
    if district is not None:
        data["district"] = district
    return data


def _stands_on_own_point(_location, latitude, longitude, **_kwargs) -> bool:
    return (latitude, longitude) == _OWN_POINT


class _Scoped(SimpleTestCase):
    """A building Location on the campus, its scope and cached rows given rather than read."""

    scope = NamingScope.BUILDING

    def setUp(self) -> None:
        super().setUp()
        self.location = Location(latitude=_LAT, longitude=_LNG)
        self.rows: dict[str, dict] = {}
        for patcher in (
            patch.object(name_tiers, "naming_scope", side_effect=lambda _location: self.scope),
            patch.object(LocationCache, "get_fresh", side_effect=self._get_fresh),
            patch.object(national_register, "stands_on", side_effect=_stands_on_own_point),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _get_fresh(self, _location, source: str, audience: str = ""):
        data = self.rows.get(source)
        return _row(data) if data is not None else None

    def _candidates(self, *providers: NameProvider) -> list[str]:
        with patch.object(plugin_registry, "name_providers", return_value=list(providers)):
            return [candidate.name for candidate in external_name_candidates_for_location(self.location)]


class TheCampusArticleNamesNoBuildingTests(_Scoped):
    def test_the_article_the_panel_fell_back_to_is_not_the_buildings_name(self) -> None:
        self.rows["cris_building_usn"] = _cris()

        names = self._candidates(_Provider("wikipedia", _CAMPUS), _Provider("cris", _BLDG33))

        self.assertEqual(names, [_BLDG33])

    def test_without_a_building_record_the_article_still_names_nothing(self) -> None:
        self.assertEqual(self._candidates(_Provider("wikipedia", _CAMPUS)), [])


class APropertyKeepsItsArticleTests(_Scoped):
    scope = NamingScope.PARCEL

    def test_a_property_is_still_named_by_its_article(self) -> None:
        self.assertEqual(self._candidates(_Provider("wikipedia", _CAMPUS)), [_CAMPUS])


class CrisRecordMustStandOnTheBuildingTests(_Scoped):
    def test_the_buildings_own_record_names_it(self) -> None:
        self.rows["cris_building_usn"] = _cris()
        self.assertTrue(building_name_admissible("cris", self.location))

    def test_a_neighbours_record_lends_no_name(self) -> None:
        """P255: the record came from a 200 m lookup with no distance limit."""
        self.rows["cris_building_usn"] = _cris(point=_NEIGHBOUR_POINT)
        self.assertFalse(building_name_admissible("cris", self.location))

    def test_a_record_with_no_position_lends_no_name(self) -> None:
        """Rows cached before positions were kept cannot be checked."""
        self.rows["cris_building_usn"] = _cris(point=None)
        self.assertFalse(building_name_admissible("cris", self.location))

    def test_another_building_source_is_unaffected(self) -> None:
        self.assertTrue(building_name_admissible("redata_building", self.location))


_HOLDING_LISTING = {
    "name": _LISTING,
    "provider": "nps_nrhp",
    "scope": "site",
    "resource_type": "national_register_listing",
    "contains_point": True,
    "source_latitude": 41.73308,
    "source_longitude": -73.92861,
}
_CRIS_DISTRICT = {"HistoricName": _LISTING, "resource_type": "national_register_listing", "contains_point": True}


class ABuildingsListingNamesTests(_Scoped):
    def test_a_listing_merely_holding_the_building_is_not_its_name(self) -> None:
        """The lead from P230: the campus listing's boundary holds most of HRSH's buildings."""
        self.rows["redata_historic_registers"] = {"resources": [_HOLDING_LISTING]}
        self.rows["cris_building_usn"] = _cris(district=_CRIS_DISTRICT)

        self.assertEqual(register_listing_names(self.location), [])

    def test_the_listing_holding_a_building_cris_calls_listed_is_its_name(self) -> None:
        self.rows["redata_historic_registers"] = {"resources": [_HOLDING_LISTING]}
        self.rows["cris_building_usn"] = _cris(eligibility="Listed", district=_CRIS_DISTRICT)

        self.assertEqual(register_listing_names(self.location), [_LISTING, _LISTING])

    def test_a_structure_listing_standing_on_the_building_is_its_name(self) -> None:
        row = {
            **_HOLDING_LISTING,
            "scope": "structure",
            "contains_point": False,
            "source_latitude": _OWN_POINT[0],
            "source_longitude": _OWN_POINT[1],
        }
        self.rows["redata_historic_registers"] = {"resources": [row]}

        self.assertEqual(register_listing_names(self.location), [_LISTING])


class APropertyKeepsTheListingHoldingItTests(_Scoped):
    scope = NamingScope.PARCEL

    def test_the_listing_holding_the_point_names_a_property(self) -> None:
        self.rows["redata_historic_registers"] = {"resources": [_HOLDING_LISTING]}
        self.rows["cris_building_usn"] = _cris(district=_CRIS_DISTRICT)

        self.assertEqual(register_listing_names(self.location), [_LISTING, _LISTING])


class CrisArrivalRefreshesNamesTests(SimpleTestCase):
    """A CRIS card landing is what can name a building; before, only a listing holding the point refreshed names."""

    def setUp(self) -> None:
        super().setUp()
        self.location = Location(latitude=_LAT, longitude=_LNG, official_name="", official_name_source="")
        self.refresh = self._patch(
            "urbanlens.dashboard.services.locations.naming.update_location_name_from_external_sources"
        )
        self._patch("urbanlens.dashboard.services.locations.register_names.register_listing_names", return_value=[])
        self._patch("django.db.transaction.on_commit", side_effect=lambda func, *args, **kwargs: func())

    def _patch(self, target: str, **kwargs):
        patcher = patch(target, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _land(self, data: dict, source: str = "cris_building_usn") -> None:
        instance = SimpleNamespace(source=source, data=data, location=self.location, location_id=None)
        cache_signals.refresh_names_on_register_listing(LocationCache, instance)

    def test_a_building_record_landing_refreshes_the_locations_names(self) -> None:
        self._land(_cris())
        self.refresh.assert_called_once_with(self.location)

    def test_a_row_naming_no_building_and_no_listing_refreshes_nothing(self) -> None:
        self._land({})
        self.refresh.assert_not_called()

    def test_a_location_the_record_already_names_is_not_refreshed_again(self) -> None:
        """A site pass rewrites every stale building card; one already named by it costs no refresh."""
        self.location.official_name = _BLDG33
        self.location.official_name_source = "cris"
        self._land(_cris())
        self.refresh.assert_not_called()

    def test_another_source_naming_a_building_is_not_this_receivers(self) -> None:
        self._land({"name": _BLDG33, "USNName": _BLDG33}, source="redata_building_attributes")
        self.refresh.assert_not_called()


class CrisNameReachesTheBuildingLocationTests(TestCase):
    """End to end on the database: a campus, a building child pin and the building's own wiki nested under the campus's."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.campus_location = baker.make(
            Location, latitude=41.73328, longitude=-73.92812, official_name="", google_place=None
        )
        self.campus = baker.make(
            Pin, profile=self.profile, location=self.campus_location, parent_pin=None, pin_type=PinType.PARCEL
        )
        self.campus_wiki, _ = Wiki.objects.get_or_create_for_location(self.campus_location)
        self.location = baker.make(Location, latitude=_LAT, longitude=_LNG, official_name="", google_place=None)
        self.child = baker.make(
            Pin,
            profile=self.profile,
            location=self.location,
            parent_pin=self.campus,
            pin_type=PinType.BUILDING,
            name=_BLDG33,
        )
        self.wiki = Wiki.objects.create(
            location=self.location,
            parent_wiki=self.campus_wiki,
            pin_type=PinType.BUILDING,
            name="Building at Hudson River State Hospital",
        )

    def _land_cris(self, **kwargs) -> None:
        with (
            patch.object(national_register, "stands_on", side_effect=_stands_on_own_point),
            self.captureOnCommitCallbacks(execute=True),
        ):
            LocationCache.set(self.location, "cris_building_usn", _cris(**kwargs), query_key="q")
        self.location.refresh_from_db()
        self.wiki.refresh_from_db()

    def test_the_cris_card_names_the_building_and_mints_its_slugs(self) -> None:
        self._land_cris()

        self.assertEqual((self.location.official_name, self.location.official_name_source), (_BLDG33, "cris"))
        self.assertFalse(is_uuid_slug(self.location.slug), self.location.slug)
        self.assertIn("bldg-33", self.location.slug)
        self.assertFalse(is_uuid_slug(self.wiki.slug), self.wiki.slug)

    def test_a_building_named_after_the_campus_article_takes_cris_s_name(self) -> None:
        LocationCache.set(
            self.location, "wikipedia", {"title": _CAMPUS, "url": "https://en.wikipedia.org/wiki/x"}, query_key="q"
        )
        Location.objects.filter(pk=self.location.pk).update(official_name=_CAMPUS, official_name_source="wikipedia")
        WikiAlias.objects.create(wiki=self.wiki, name=_CAMPUS, kind="official", source="wikipedia")

        self._land_cris()

        self.assertEqual((self.location.official_name, self.location.official_name_source), (_BLDG33, "cris"))
        self.assertFalse(
            WikiAlias.objects.filter(wiki=self.wiki, name=_CAMPUS).exists(),
            "the campus's name is no alias of the building",
        )

    def test_the_campus_article_alone_leaves_the_building_unnamed(self) -> None:
        LocationCache.set(
            self.location, "wikipedia", {"title": _CAMPUS, "url": "https://en.wikipedia.org/wiki/x"}, query_key="q"
        )

        update_location_name_from_external_sources(self.location)

        self.location.refresh_from_db()
        self.assertEqual(self.location.official_name or "", "")
        self.assertTrue(is_uuid_slug(self.location.slug))
