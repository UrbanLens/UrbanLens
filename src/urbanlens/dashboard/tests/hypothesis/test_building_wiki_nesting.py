"""A building's wiki nests under its parcel's from what the places say about its location, never from pins (P231).

A child pin's wiki was created as a root by ``tasks.ensure_wiki_for_location`` and nested only when the location's
boundary generation ran, which waits for an owner who allows enrichment. A wiki holding no place of its own then
looked for its container on the most specific place its point resolved onto: a building place with no wiki, so
none was found.
"""

from __future__ import annotations

import io
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.test import override_settings
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import ImageSource
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.photos import photo_enrichment
from urbanlens.dashboard.services.places import lineage
from urbanlens.dashboard.services.wiki import building_wikis, wiki_merge
from urbanlens.dashboard.services.wiki.wiki_merge import reconcile_wiki_nesting
from urbanlens.dashboard.services.wiki.wiki_share import WikiShareService
from urbanlens.dashboard.tasks import ensure_wiki_for_location

from .place_helpers import make_place

_LAT, _LNG = 41.7333, -73.9281
_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-media-")


def _square(latitude: float, longitude: float, half: float) -> MultiPolygon:
    ring = (
        (longitude - half, latitude - half),
        (longitude + half, latitude - half),
        (longitude + half, latitude + half),
        (longitude - half, latitude + half),
        (longitude - half, latitude - half),
    )
    return MultiPolygon(Polygon(ring), srid=4326)


class BuildingWikiNestingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.parcel = make_place(PlaceKind.PARCEL, _square(_LAT, _LNG, 0.002), name="Campus parcel")
        self.campus_location = baker.make(Location, latitude=_LAT, longitude=_LNG, google_place=None)
        self.campus_wiki, _ = Wiki.objects.get_or_create_for_location(self.campus_location)
        self.building = make_place(PlaceKind.BUILDING, _square(_LAT + 0.001, _LNG + 0.001, 0.0002), parent=self.parcel)
        self.location = baker.make(Location, latitude=_LAT + 0.001, longitude=_LNG + 0.001, google_place=None)

    def test_the_fixture_stands_the_building_on_its_parcel(self) -> None:
        self.assertEqual(self.campus_location.place_id, self.parcel.pk)
        self.assertEqual(self.campus_wiki.place_id, self.parcel.pk)
        self.assertEqual(self.location.place_id, self.building.pk)

    def test_a_child_pins_new_wiki_nests_under_the_parcels_without_enrichment(self) -> None:
        campus_pin = baker.make(Pin, profile=self.profile, location=self.campus_location, parent_pin=None)
        baker.make(Pin, profile=self.profile, location=self.location, parent_pin=campus_pin, pin_type=PinType.BUILDING)
        self.profile.external_apis_enabled = False
        self.profile.save(update_fields=["external_apis_enabled"])

        wiki_pk = ensure_wiki_for_location(self.location.pk)

        wiki = Wiki.objects.get(pk=wiki_pk)
        self.assertEqual(wiki.location_id, self.location.pk)
        self.assertEqual(wiki.parent_wiki_id, self.campus_wiki.pk)

    def test_a_share_that_creates_the_buildings_wiki_nests_it(self) -> None:
        """P263: a share racing ahead of ``ensure_wiki_for_location`` creates the wiki itself."""
        pin = baker.make(Pin, profile=self.profile, location=self.location, parent_pin=None)
        self.assertFalse(Wiki.objects.filter(location=self.location).exists())

        wiki, _shared = WikiShareService().share_from_pin(pin)

        self.assertEqual(wiki.location_id, self.location.pk)
        self.assertEqual(Wiki.objects.get(pk=wiki.pk).parent_wiki_id, self.campus_wiki.pk)

    @override_settings(MEDIA_ROOT=_MEDIA_ROOT)
    def test_an_enrichment_photo_that_creates_the_buildings_wiki_nests_it(self) -> None:
        """P263."""
        buffer = io.BytesIO()
        PILImage.new("RGB", (8, 8)).save(buffer, format="JPEG")

        with patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"):
            image = photo_enrichment._save_enriched_image(
                self.location, buffer.getvalue(), source=ImageSource.GOOGLE_MAPS, max_dimension=800
            )

        self.assertEqual(image.wiki.location_id, self.location.pk)
        self.assertEqual(Wiki.objects.get(pk=image.wiki_id).parent_wiki_id, self.campus_wiki.pk)

    def test_a_placeless_wiki_on_a_building_with_no_wiki_finds_the_parcels(self) -> None:
        """No pin anywhere: the places alone decide."""
        wiki = Wiki.objects.create(location=self.location, name="BLDG 33/POWERHOUSE & MACHINE SHOP (1929)")
        Wiki.objects.filter(pk=wiki.pk).update(place=None)
        wiki.refresh_from_db()

        reconcile_wiki_nesting(wiki)

        wiki.refresh_from_db()
        self.assertEqual(wiki.parent_wiki_id, self.campus_wiki.pk)


class _Wikis:
    """``Wiki.objects`` over a fixed list, for the lookups the container search makes."""

    def __init__(self, wikis: list[SimpleNamespace]) -> None:
        self.wikis = wikis

    def filter(self, **lookups) -> _Wikis:
        rows = self.wikis
        if "place" in lookups:
            rows = [wiki for wiki in rows if wiki.place_id == lookups["place"].pk]
        if "place_id__in" in lookups:
            rows = [wiki for wiki in rows if wiki.place_id in lookups["place_id__in"]]
        if lookups.get("parent_wiki__isnull"):
            rows = [wiki for wiki in rows if wiki.parent_wiki_id is None]
        return _Wikis(rows)

    def exclude(self, *, pk: int) -> _Wikis:
        return _Wikis([wiki for wiki in self.wikis if wiki.pk != pk])

    def select_related(self, *_fields: str) -> _Wikis:
        return self

    def first(self) -> SimpleNamespace | None:
        return self.wikis[0] if self.wikis else None

    def __iter__(self):
        return iter(self.wikis)


class ContainerFromThePlacesTests(SimpleTestCase):
    """The container search for a wiki holding no place, on stand-in rows."""

    def test_a_building_place_with_no_wiki_leads_up_to_its_parcels(self) -> None:
        parcel = SimpleNamespace(pk=1)
        building = SimpleNamespace(pk=2)
        parcel_wiki = SimpleNamespace(pk=10, place_id=parcel.pk, parent_wiki_id=None)
        location = SimpleNamespace(
            point=Point(_LNG, _LAT, srid=4326), place_id=building.pk, place=building, latitude=_LAT, longitude=_LNG
        )
        wiki = SimpleNamespace(pk=11, place_id=None, place=None, location_id=5, location=location, parent_wiki_id=None)

        with (
            patch.object(Wiki, "objects", _Wikis([parcel_wiki, wiki])),
            patch.object(Place.objects, "resolve_for_point", return_value=building),
            patch.object(lineage, "ancestors_of", side_effect=lambda place: [parcel] if place is building else []),
            # Not a campus: no building of the parcel's decides instead.
            patch.object(building_wikis, "standing_building", return_value=None),
        ):
            container = wiki_merge._containing_root_wiki_by_geometry(wiki)

        self.assertIs(container, parcel_wiki)
