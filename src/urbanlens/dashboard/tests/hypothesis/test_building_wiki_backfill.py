"""A pinned location on a campus building gets the building's wiki once the campus's building list says where it stands (P265).

A location pinned before the list was cached was answered with the campus's wiki, so since P261 it links none.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.parcel_buildings import (
    ParcelBuildingsEnrichmentSource,
    ParcelBuildingsPanelSource,
)
from urbanlens.dashboard.tasks import ensure_building_wikis

from .test_building_wiki_resolution import _B, _C, _F, _LAT, _LNG, _Campus

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_MODULE = "urbanlens.dashboard.plugins.builtin.parcel_buildings"


class _PinnedCampus(_Campus):
    def pinned_at(self, latitude: float, longitude: float, *, community: bool = True) -> Location:
        """A location some profile pinned before anything gave it a wiki."""
        location = self.location_at(latitude, longitude)
        owner = baker.make(User).profile
        if not community:
            Profile.objects.filter(pk=owner.pk).update(community_enabled=False)
        baker.make(Pin, profile=Profile.objects.get(pk=owner.pk), location=location, parent_pin=None, wiki=None)
        return location


class EnsureBuildingWikisTests(_PinnedCampus):
    def test_a_pinned_location_on_a_building_without_a_wiki_gets_the_building_s(self) -> None:
        location = self.pinned_at(_C[0], _C[1] + 0.00003)

        ensure_building_wikis(self.campus_location.pk)

        wiki = Wiki.objects.existing_for_location(location)
        self.assertIsNotNone(wiki)
        assert wiki is not None
        self.assertNotEqual(wiki.pk, self.campus_wiki.pk)
        self.assertEqual(wiki.pin_type, PinType.BUILDING)
        self.assertEqual(wiki.parent_wiki_id, self.campus_wiki.pk)

    def test_a_wing_s_wiki_nests_under_its_envelope_s(self) -> None:
        location = self.pinned_at(_F[0] + 0.00002, _F[1])

        ensure_building_wikis(self.campus_location.pk)

        wiki = Wiki.objects.existing_for_location(location)
        self.assertIsNotNone(wiki)
        assert wiki is not None
        self.assertEqual(wiki.parent_wiki_id, self.wiki_e.pk)

    def test_two_pinned_locations_on_one_building_share_its_one_new_wiki(self) -> None:
        first = self.pinned_at(_C[0], _C[1] + 0.00003)
        second = self.pinned_at(_C[0] + 0.00003, _C[1])

        ensure_building_wikis(self.campus_location.pk)

        wiki = Wiki.objects.existing_for_location(first)
        self.assertIsNotNone(wiki)
        self.assertEqual(Wiki.objects.existing_for_location(second), wiki)

    def test_the_grounds_and_a_building_with_a_wiki_create_none(self) -> None:
        self.pinned_at(_LAT - 0.0015, _LNG + 0.0015)
        self.pinned_at(_B[0] + 0.00005, _B[1])
        before = Wiki.objects.count()

        self.assertEqual(ensure_building_wikis(self.campus_location.pk), [])
        self.assertEqual(Wiki.objects.count(), before)

    def test_a_location_nobody_pinned_gets_none(self) -> None:
        location = self.location_at(_C[0], _C[1] + 0.00003)

        ensure_building_wikis(self.campus_location.pk)

        self.assertIsNone(Wiki.objects.existing_for_location(location))

    def test_a_location_pinned_only_with_community_features_off_gets_none(self) -> None:
        """As on a pin save: such an owner's pin makes no wiki."""
        location = self.pinned_at(_C[0], _C[1] + 0.00003, community=False)

        ensure_building_wikis(self.campus_location.pk)

        self.assertIsNone(Wiki.objects.existing_for_location(location))

    def test_a_location_holding_no_campus_wiki_does_nothing(self) -> None:
        location = self.pinned_at(_C[0], _C[1] + 0.00003)

        self.assertEqual(ensure_building_wikis(self.wiki_b.location_id), [])
        self.assertIsNone(Wiki.objects.existing_for_location(location))


class BuildingListCachedTests(_PinnedCampus):
    """Each path that caches a campus's building list asks for its pinned buildings' wikis."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch(f"{_MODULE}.fetch_parcel_buildings", return_value={"buildings": []}))
        self.enterContext(patch("urbanlens.dashboard.services.pins.auto_nest.auto_nest_location"))
        self.enqueue = self.enterContext(patch(_ENQUEUE))

    def _enqueued(self) -> list[tuple]:
        return [
            call.args for call in self.enqueue.call_args_list if call.args and call.args[0] is ensure_building_wikis
        ]

    def test_the_panel_fetch_asks_after_the_list_lands(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=self.campus_location, parent_pin=None)

        with self.captureOnCommitCallbacks(execute=True):
            ParcelBuildingsPanelSource().fetch(pin)

        self.assertEqual(self._enqueued(), [(ensure_building_wikis, self.campus_location.pk)])

    def test_background_enrichment_asks_after_the_list_lands(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            ParcelBuildingsEnrichmentSource().enrich(self.campus_location)

        self.assertEqual(self._enqueued(), [(ensure_building_wikis, self.campus_location.pk)])

    def test_a_list_for_a_location_holding_no_campus_wiki_asks_nothing(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            ParcelBuildingsEnrichmentSource().enrich(self.wiki_b.location)

        self.assertEqual(self._enqueued(), [])
