"""P231/P255's data migration: existing building locations lose the campus's name and take their own CRIS record's.

On the dev stack ten building locations under HRSH were named "Hudson River State Hospital" from Wikipedia, with
slugs minted from it, and most others had no provider name though a CRIS card standing on them named the building.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.location.slug_history import LocationSlugHistory
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.core.slugs import is_uuid_slug

from .place_helpers import make_place

migration = importlib.import_module("urbanlens.dashboard.migrations.0049_building_location_names")

_CAMPUS = "Hudson River State Hospital"
_BLDG33 = "BLDG 33/POWERHOUSE & MACHINE SHOP (1929)"
_LAT, _LNG = 41.7333, -73.9281
_METRE = 1 / 111_320


def _square(latitude: float, longitude: float, half: float) -> MultiPolygon:
    ring = (
        (longitude - half, latitude - half),
        (longitude + half, latitude - half),
        (longitude + half, latitude + half),
        (longitude - half, latitude + half),
        (longitude - half, latitude - half),
    )
    return MultiPolygon(Polygon(ring), srid=4326)


_HISTORICAL_APPS = None


def _historical_apps():
    """The models as the migration sees them under ``migrate``: no properties, no custom managers."""
    global _HISTORICAL_APPS  # noqa: PLW0603 - rendering the state takes seconds; one per test run
    if _HISTORICAL_APPS is None:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        node = ("dashboard", migration.Migration.dependencies[0][1])
        _HISTORICAL_APPS = MigrationExecutor(connection).loader.project_state(node).apps
    return _HISTORICAL_APPS


class BuildingLocationNamesMigrationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.campus_location = baker.make(Location, latitude=_LAT, longitude=_LNG, google_place=None)
        Location.objects.filter(pk=self.campus_location.pk).update(
            official_name=_CAMPUS, official_name_source="wikipedia", slug="hudson-river-state-hospital"
        )
        self.campus = baker.make(
            Pin, profile=self.profile, location=self.campus_location, parent_pin=None, pin_type=PinType.PARCEL
        )
        self._seq = 0

    def _building(self, *, name: str = "", source: str = "", slug: str | None = None) -> Location:
        self._seq += 1
        location = baker.make(
            Location, latitude=_LAT + self._seq * 0.0005, longitude=_LNG + self._seq * 0.0005, google_place=None
        )
        Location.objects.filter(pk=location.pk).update(
            official_name=name, official_name_source=source, slug=slug or str(location.uuid)
        )
        baker.make(Pin, profile=self.profile, location=location, parent_pin=self.campus, pin_type=PinType.BUILDING)
        location.refresh_from_db()
        return location

    def _cris(self, location: Location, *, metres_north: float | None = 3.0) -> None:
        data: dict = {"USNName": _BLDG33, "resource_uuid": "b-33"}
        if metres_north is not None:
            data.update(
                source_latitude=float(location.latitude) + metres_north * _METRE,
                source_longitude=float(location.longitude),
            )
        LocationCache.objects.create(location=location, source="cris_building_usn", data=data)

    def _run(self) -> None:
        migration.fix_building_locations(_historical_apps(), None)

    def test_a_campus_article_name_gives_way_to_redata_s_building_name(self) -> None:
        """Reaches the address-fragment check, which reads properties a migration's models do not have."""
        location = self._building(name=_CAMPUS, source="wikipedia", slug="hudson-river-state-hospital-32656")
        LocationCache.objects.create(location=location, source="redata_building_attributes", data={"name": "Kirkbride"})

        self._run()

        location.refresh_from_db()
        self.assertEqual((location.official_name, location.official_name_source), ("Kirkbride", "redata_building"))

    def test_a_neighbours_record_ten_metres_off_names_nothing(self) -> None:
        """Within reach of both, a record belongs to the building nearer it, as the runtime rule has it."""
        own = self._building()
        neighbour = baker.make(
            Location, latitude=float(own.latitude) + 10 * _METRE, longitude=float(own.longitude), google_place=None
        )
        baker.make(Pin, profile=self.profile, location=neighbour, parent_pin=self.campus, pin_type=PinType.BUILDING)
        buildings = [
            {"latitude": float(spot.latitude), "longitude": float(spot.longitude)} for spot in (own, neighbour)
        ]
        LocationCache.objects.create(
            location=self.campus_location, source="parcel_buildings", data={"buildings": buildings}
        )
        LocationCache.objects.create(
            location=own,
            source="cris_building_usn",
            data={
                "USNName": _BLDG33,
                "resource_uuid": "b-33",
                "source_latitude": float(neighbour.latitude),
                "source_longitude": float(neighbour.longitude),
            },
        )

        self._run()

        own.refresh_from_db()
        self.assertEqual(own.official_name or "", "")
        self.assertFalse(LocationCache.objects.filter(location=own, source="cris_building_usn").exists())

    def test_a_building_named_after_the_campus_article_takes_its_cris_name_and_slug(self) -> None:
        location = self._building(name=_CAMPUS, source="wikipedia", slug="hudson-river-state-hospital-32655")
        self._cris(location)
        wiki = Wiki.objects.create(location=location, name="Building at Hudson River State Hospital")
        Wiki.objects.filter(pk=wiki.pk).update(slug="hrsh-hudson-river-state-hospital")

        self._run()

        location.refresh_from_db()
        wiki.refresh_from_db()
        self.assertEqual((location.official_name, location.official_name_source), (_BLDG33, "cris"))
        self.assertTrue(location.slug.startswith("bldg-33"), location.slug)
        self.assertIn(
            "hudson-river-state-hospital-32655",
            LocationSlugHistory.objects.filter(location=location).values_list("slug", flat=True),
        )
        self.assertNotIn("hudson-river-state-hospital", wiki.slug)

    def test_a_campus_article_name_with_no_record_behind_it_is_cleared(self) -> None:
        location = self._building(name=_CAMPUS, source="wikipedia", slug="hudson-river-state-hospital-85062")

        self._run()

        location.refresh_from_db()
        self.assertEqual((location.official_name, location.official_name_source), ("", ""))
        self.assertTrue(is_uuid_slug(location.slug))
        self.assertTrue(
            LocationSlugHistory.objects.filter(location=location, slug="hudson-river-state-hospital-85062").exists()
        )

    def test_an_unnamed_building_takes_its_own_records_name(self) -> None:
        location = self._building()
        self._cris(location)

        self._run()

        location.refresh_from_db()
        self.assertEqual((location.official_name, location.official_name_source), (_BLDG33, "cris"))
        self.assertFalse(is_uuid_slug(location.slug))

    def test_the_buildings_own_record_names_it_ahead_of_redata(self) -> None:
        location = self._building()
        self._cris(location)
        LocationCache.objects.create(
            location=location, source="redata_building_attributes", data={"name": "POWERHOUSE"}
        )

        self._run()

        location.refresh_from_db()
        self.assertEqual((location.official_name, location.official_name_source), (_BLDG33, "cris"))

    def test_a_neighbours_record_names_nothing_and_is_fetched_again(self) -> None:
        location = self._building(name=_BLDG33, source="cris", slug="bldg-33powerhouse-machine-shop-1929")
        self._cris(location, metres_north=40.0)

        self._run()

        location.refresh_from_db()
        self.assertEqual(location.official_name_source, "")
        self.assertTrue(is_uuid_slug(location.slug))
        self.assertFalse(LocationCache.objects.filter(location=location, source="cris_building_usn").exists())

    def test_a_record_with_no_position_is_fetched_again(self) -> None:
        location = self._building()
        self._cris(location, metres_north=None)

        self._run()

        location.refresh_from_db()
        self.assertTrue(is_uuid_slug(location.slug))
        self.assertFalse(LocationCache.objects.filter(location=location, source="cris_building_usn").exists())

    def test_a_property_keeps_its_article_name(self) -> None:
        self._run()

        self.campus_location.refresh_from_db()
        self.assertEqual(
            (self.campus_location.official_name, self.campus_location.slug), (_CAMPUS, "hudson-river-state-hospital")
        )

    def test_a_root_wiki_on_a_building_place_nests_under_the_parcels(self) -> None:
        parcel = make_place(PlaceKind.PARCEL, _square(_LAT, _LNG, 0.004))
        building = make_place(PlaceKind.BUILDING, _square(_LAT + 0.0005, _LNG + 0.0005, 0.0001), parent=parcel)
        campus_wiki = Wiki.objects.create(location=self.campus_location, name=_CAMPUS)
        Wiki.objects.filter(pk=campus_wiki.pk).update(place=parcel)
        location = self._building()
        Location.objects.filter(pk=location.pk).update(place=building)
        wiki = Wiki.objects.create(location=location, name=_BLDG33)
        Wiki.objects.filter(pk=wiki.pk).update(place=building, parent_wiki=None)

        self._run()

        wiki.refresh_from_db()
        self.assertEqual(wiki.parent_wiki_id, campus_wiki.pk)


class NameChoiceTests(SimpleTestCase):
    """The name the migration gives one building location, from its cached rows."""

    def _location(self, name: str = "", source: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            official_name=name,
            official_name_source=source,
            place=None,
            latitude=_LAT,
            longitude=_LNG,
            # A migration's models carry fields only, never Location's city/state/address properties.
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            zipcode="",
            street_number="",
            route="",
        )

    def _cris(self, *, metres_north: float = 3.0) -> dict:
        return {"USNName": _BLDG33, "source_latitude": _LAT + metres_north * _METRE, "source_longitude": _LNG}

    def test_the_buildings_own_record_names_it_ahead_of_redata(self) -> None:
        """The runtime resolver prefers CRIS's record on a child pin's location, as the pin was named."""
        decided = migration._new_name(self._location(), self._cris(), None, {"name": "POWERHOUSE & MACHINE SHOP"})

        self.assertEqual(decided, (_BLDG33, "cris"))

    def test_a_campus_name_with_no_record_standing_on_the_building_gives_way_to_redata_s(self) -> None:
        """Clearing it would turn a readable slug into the uuid until something next names the location."""
        decided = migration._new_name(
            self._location(_CAMPUS, "wikipedia"), self._cris(metres_north=60.0), None, {"name": "Kirkbride"}
        )

        self.assertEqual(decided, ("Kirkbride", "redata_building"))

    def test_an_address_fragment_from_redata_names_nothing(self) -> None:
        decided = migration._new_name(self._location(_CAMPUS, "wikipedia"), None, None, {"name": "Poughkeepsie"})

        self.assertEqual(decided, ("", ""))

    def test_an_unnamed_building_with_only_redata_s_name_is_left_to_the_runtime(self) -> None:
        self.assertIsNone(migration._new_name(self._location(), None, None, {"name": "GARAGE"}))
