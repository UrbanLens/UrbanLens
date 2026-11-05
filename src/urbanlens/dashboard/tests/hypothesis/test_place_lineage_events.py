"""Split detection for keyless providers, and the single handler that turns lineage events into grants."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.model import GrantReason, Place, PlaceAccessGrant, PlaceKind, PlaceStatus
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries
from urbanlens.dashboard.services.places import provisioning
from urbanlens.dashboard.services.places.lineage_events import LineageEvent, LineageEventKind, handle_lineage_event
from urbanlens.dashboard.services.places.splits import looks_like_a_split

from .test_places_access_predicate import pin_on
from .test_places_campus import make_place

WEST, SOUTH, SIZE = -74.0, 40.0, 0.002
_CHAIN = "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain"


def _strip(start: float, end: float, *, height: float = SIZE) -> MultiPolygon:
    """The part of the old parcel between two fractions of its width."""
    west, east = WEST + start * SIZE, WEST + end * SIZE
    ring = ((west, SOUTH), (east, SOUTH), (east, SOUTH + height), (west, SOUTH + height), (west, SOUTH))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class _OldParcelCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.holder = baker.make(User).profile
        self.old = make_place(PlaceKind.PARCEL, _strip(0, 1), name="Old")
        Place.objects.filter(pk=self.old.pk).update(geometry_generated_at=timezone.now() - timedelta(days=1))
        self.old.refresh_from_db()
        self.pin = pin_on(self.holder, self.old, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.2)

    def remainder_parcel(self, start: float = 0.6, end: float = 1.0) -> Place:
        return make_place(PlaceKind.PARCEL, _strip(start, end), name="Remainder")


class MildShrinkTests(_OldParcelCase):
    def test_a_60_40_split_whose_remainder_is_its_own_parcel_grants_both_successors(self) -> None:
        remainder = self.remainder_parcel()
        kept = _strip(0, 0.6)
        self.assertTrue(looks_like_a_split(self.old, kept))

        with mock.patch(_CHAIN):
            superseded = provisioning.detect_subdivision(self.pin.location, kept)

        self.assertEqual(superseded, self.old)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.SUPERSEDED)
        successors = set(self.old.children.values_list("pk", flat=True))
        self.assertIn(remainder.pk, successors)
        self.assertEqual(len(successors), 2)
        granted = set(
            PlaceAccessGrant.objects.filter(profile=self.holder, reason=GrantReason.GRANDFATHERED_SPLIT).values_list(
                "place_id", flat=True
            )
        )
        self.assertEqual(granted, {self.old.pk, *successors})

    def test_a_60_40_shrink_with_nothing_else_there_is_not_a_split(self) -> None:
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))
        with mock.patch(_CHAIN):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6)))
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_pin_outside_the_new_outline_is_not_a_signal(self) -> None:
        outside = pin_on(self.holder, self.old, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.9)
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))
        with mock.patch(_CHAIN):
            self.assertIsNone(provisioning.detect_subdivision(outside.location, _strip(0, 0.6)))
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_95_percent_nudge_grants_nothing_even_beside_another_parcel(self) -> None:
        self.remainder_parcel(0.95, 1.0)
        nudged = _strip(0, 0.95)
        self.assertFalse(looks_like_a_split(self.old, nudged))
        with mock.patch(_CHAIN):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, nudged))
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_remainder_mostly_uncovered_is_not_a_split(self) -> None:
        self.remainder_parcel(0.9, 1.0)  # covers a quarter of the missing 40%
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))

    def test_a_parcel_that_predates_the_old_geometry_does_not_count(self) -> None:
        remainder = self.remainder_parcel()
        Place.objects.filter(pk=remainder.pk).update(created=timezone.now() - timedelta(days=2))
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))

    def test_a_parcel_seen_in_the_same_refresh_counts(self) -> None:
        self.assertTrue(looks_like_a_split(self.old, _strip(0, 0.6), seen=[_strip(0.6, 1.0)]))

    def test_another_providers_overlapping_neighbour_is_no_split_grant_or_reparent(self) -> None:
        neighbour = self.remainder_parcel(0.55, 1.0)
        Place.objects.filter(pk=neighbour.pk).update(provider="other")
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))
        with mock.patch(_CHAIN):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6)))
        neighbour.refresh_from_db()
        self.assertIsNone(neighbour.parent_id)
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_neighbour_overlapping_the_new_outline_is_not_the_remainder(self) -> None:
        self.remainder_parcel(0.4, 1.0)
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))

    def test_a_neighbour_that_already_has_a_parent_is_untouched(self) -> None:
        site = make_place(PlaceKind.PARCEL, _strip(2, 3), name="Site")
        neighbour = self.remainder_parcel()
        Place.objects.filter(pk=neighbour.pk).update(parent=site)
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))
        neighbour.refresh_from_db()
        self.assertEqual(neighbour.parent_id, site.pk)

    def test_a_neighbour_whose_domain_has_strangers_pins_is_skipped(self) -> None:
        neighbour = self.remainder_parcel()
        pin_on(baker.make(User).profile, neighbour, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.8)
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6)))

    def test_a_probe_answered_with_a_neighbour_holding_strangers_pins_neither_splits_nor_reparents(self) -> None:
        neighbour = self.remainder_parcel()
        pin_on(baker.make(User).profile, neighbour, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.8)
        pin_on(self.holder, self.old, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.9)
        with mock.patch(_CHAIN) as chain:
            chain.return_value.get_boundaries.return_value = ResolvedBoundaries(property_polygon=_strip(0.6, 1.0))
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6)))
        chain.return_value.get_boundaries.assert_called()
        neighbour.refresh_from_db()
        self.assertIsNone(neighbour.parent_id)
        self.assertFalse(PlaceAccessGrant.objects.filter(profile=self.holder).exists())

    def test_a_heavy_shrink_probe_answered_with_a_parented_neighbour_leaves_it_alone(self) -> None:
        site = make_place(PlaceKind.PARCEL, _strip(2, 3), name="Site")
        neighbour = self.remainder_parcel(0.2, 1.0)
        Place.objects.filter(pk=neighbour.pk).update(parent=site)
        pin_on(self.holder, self.old, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.9)
        with mock.patch(_CHAIN) as chain:
            chain.return_value.get_boundaries.return_value = ResolvedBoundaries(property_polygon=_strip(0.2, 1.0))
            provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2))
        chain.return_value.get_boundaries.assert_called()
        neighbour.refresh_from_db()
        self.assertEqual(neighbour.parent_id, site.pk)
        self.assertFalse(PlaceAccessGrant.objects.filter(profile=self.holder, place=neighbour).exists())

    def test_the_pieces_must_rebuild_most_of_the_old_outline(self) -> None:
        self.assertTrue(looks_like_a_split(self.old, _strip(0, 0.6), seen=[_strip(0.6, 0.85)]))
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.5), seen=[_strip(0.5, 0.75)]))

    def test_a_stored_remainder_means_no_provider_is_asked(self) -> None:
        self.remainder_parcel()
        other = pin_on(self.holder, self.old, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.9)
        self.assertTrue(other.location.place_id)
        with mock.patch(_CHAIN) as chain:
            provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6))
        chain.assert_not_called()

    def test_a_shrink_under_34_percent_still_splits_alone(self) -> None:
        self.assertTrue(looks_like_a_split(self.old, _strip(0, 0.2)))
        with mock.patch(_CHAIN):
            provisioning.detect_subdivision(
                self.pin.location, _strip(0, 0.2)
            )  # only one successor found: stays current
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.CURRENT)


class LineageHandlerTests(_OldParcelCase):
    def _event(self, kind: LineageEventKind, learned_at=None) -> tuple[LineageEvent, Place]:
        successor = make_place(PlaceKind.PARCEL, _strip(0, 0.5), name="Successor")
        return LineageEvent(kind, [self.old], [successor], learned_at or timezone.now()), successor

    def test_it_grants_holders_the_predecessor_and_successor(self) -> None:
        event, successor = self._event(LineageEventKind.SPLIT)
        self.assertEqual(handle_lineage_event(event), 1)
        grants = PlaceAccessGrant.objects.filter(profile=self.holder)
        self.assertEqual({g.place_id for g in grants}, {self.old.pk, successor.pk})
        self.assertEqual({g.reason for g in grants}, {GrantReason.GRANDFATHERED_SPLIT})

    def test_merge_and_reclassify_use_the_lineage_reason(self) -> None:
        event, _ = self._event(LineageEventKind.MERGE)
        handle_lineage_event(event)
        self.assertEqual({g.reason for g in PlaceAccessGrant.objects.all()}, {GrantReason.GRANDFATHERED_LINEAGE})

    def test_pins_made_after_we_learned_of_the_event_earn_nothing(self) -> None:
        event, _ = self._event(LineageEventKind.SPLIT, learned_at=timezone.now() - timedelta(hours=1))
        late = baker.make(User).profile
        pin_on(late, self.old, lat=SOUTH + 0.0001, lng=WEST + 0.0001)
        handle_lineage_event(event)
        self.assertFalse(PlaceAccessGrant.objects.filter(profile=late).exists())

    def test_repeating_an_event_is_harmless_and_nothing_is_removed(self) -> None:
        event, _ = self._event(LineageEventKind.SPLIT)
        handle_lineage_event(event)
        before = PlaceAccessGrant.objects.count()
        self.pin.delete()
        handle_lineage_event(event)
        self.assertEqual(PlaceAccessGrant.objects.count(), before)
