"""Split detection for keyless providers, and the single handler that turns lineage events into grants."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSException, MultiPolygon, Polygon
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import (
    GrantReason,
    Place,
    PlaceAccessGrant,
    PlaceKind,
    PlaceRelation,
    PlaceStatus,
)
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries
from urbanlens.dashboard.services.places import provisioning
from urbanlens.dashboard.services.places.lineage_events import (
    LineageEvent,
    LineageEventKind,
    handle_lineage_event,
    holders_of,
)
from urbanlens.dashboard.services.places.splits import looks_like_a_split, process_split

from .test_places_access_predicate import pin_on
from .test_places_campus import make_place

WEST, SOUTH, SIZE = -74.0, 40.0, 0.002
_CHAIN = "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain"


def _strip(start: float, end: float, *, height: float = SIZE) -> MultiPolygon:
    """The part of the old parcel between two fractions of its width."""
    west, east = WEST + start * SIZE, WEST + end * SIZE
    ring = ((west, SOUTH), (east, SOUTH), (east, SOUTH + height), (west, SOUTH + height), (west, SOUTH))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


def _each_holding_the_others_centroid() -> tuple[MultiPolygon, MultiPolygon]:
    """Two disjoint multi-part outlines that find_matching_place takes for the same parcel, by mutual centroid containment."""
    first = MultiPolygon(*_strip(0, 0.2), *_strip(0.6, 0.8), srid=4326)
    second = MultiPolygon(*_strip(0.35, 0.45), *_strip(0.8, 1.0), srid=4326)
    return first, second


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

    def test_an_outline_seen_in_the_same_refresh_answers_to_the_stored_parcels_rule(self) -> None:
        # Fills the whole remainder, but overlaps the new outline: a different parcel, not the rest of this one.
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6), seen=[_strip(0.3, 1.0)]))
        # Beside the old outline rather than in its remainder.
        self.assertFalse(looks_like_a_split(self.old, _strip(0, 0.6), seen=[_strip(0.6, 1.8)]))

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

    def test_a_shrink_under_34_percent_counts_without_a_remainder_parcel(self) -> None:
        self.assertTrue(looks_like_a_split(self.old, _strip(0, 0.2)))
        with mock.patch(_CHAIN):
            # Nothing else to probe, so there is only one successor: it stays current.
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)))
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.CURRENT)

    def test_a_stored_neighbour_only_touching_the_old_outline_is_not_a_successor(self) -> None:
        remainder = self.remainder_parcel()
        touching = self.remainder_parcel(1.0, 1.4)
        with mock.patch(_CHAIN):
            self.assertEqual(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6)), self.old)
        touching.refresh_from_db()
        self.assertIsNone(touching.parent_id)
        self.assertEqual(
            set(self.old.children.values_list("pk", flat=True)) & {remainder.pk, touching.pk}, {remainder.pk}
        )
        self.assertFalse(PlaceAccessGrant.objects.filter(place=touching).exists())


def _chain_answering(outline: MultiPolygon) -> type:
    """A provider chain that answers every probe with ``outline``, from the parcel's own (empty) provider."""

    class _Chain:
        def get_boundaries(self, latitude: float, longitude: float, *, name: str | None = None) -> ResolvedBoundaries:
            return ResolvedBoundaries(property_polygon=outline, property_candidates=[("", outline)])

    return _Chain


class ProbedSuccessorTests(_OldParcelCase):
    """A probed outline answers to the same rules as a stored one."""

    def setUp(self) -> None:
        super().setUp()
        self.other = pin_on(self.holder, self.old, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.9)

    def test_a_heavy_shrink_splits_when_a_probe_finds_the_remainder(self) -> None:
        with mock.patch(_CHAIN, _chain_answering(_strip(0.2, 1.0))):
            self.assertEqual(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)), self.old)
        self.assertEqual(self.old.children.count(), 2)

    def test_a_probe_answering_with_a_parcel_beside_the_old_outline_is_no_successor(self) -> None:
        with mock.patch(_CHAIN, _chain_answering(_strip(1.0, 1.8))):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)))
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.CURRENT)
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_probe_mostly_outside_the_remainder_is_no_successor(self) -> None:
        with mock.patch(_CHAIN, _chain_answering(_strip(0.8, 2.0))):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)))
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_probe_describing_a_parcel_of_another_family_is_no_successor(self) -> None:
        site = make_place(PlaceKind.PARCEL, _strip(2, 3), name="Site")
        taken = make_place(
            PlaceKind.PARCEL, _strip(0.2, 1.0), parent=site, relation=PlaceRelation.MEMBER_OF, name="Taken"
        )
        with mock.patch(_CHAIN, _chain_answering(_strip(0.2, 1.0))):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)))
        taken.refresh_from_db()
        self.assertEqual(taken.parent_id, site.pk)
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_probe_describing_a_parcel_with_strangers_pins_is_no_successor(self) -> None:
        theirs = make_place(PlaceKind.PARCEL, _strip(0.2, 1.0), name="Theirs")
        pin_on(baker.make(User).profile, theirs, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.5)
        with mock.patch(_CHAIN, _chain_answering(_strip(0.2, 1.0))):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)))
        theirs.refresh_from_db()
        self.assertIsNone(theirs.parent_id)
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_nulled_domain_root_limits_probes_to_the_parcels_own_pins(self) -> None:
        Place.objects.filter(pk=self.old.pk).update(domain_root=None)
        self.old.refresh_from_db()
        elsewhere = make_place(PlaceKind.PARCEL, _strip(5, 6), name="Elsewhere")
        Place.objects.filter(pk=elsewhere.pk).update(domain_root=None)
        pin_on(baker.make(User).profile, elsewhere, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 5.5)
        placeless = baker.make(Location, latitude=SOUTH + SIZE / 2, longitude=WEST + SIZE * 7.5)
        self.assertIsNone(placeless.place_id)

        probed: list[float] = []

        class _Recording(_chain_answering(_strip(0.2, 1.0))):
            def get_boundaries(
                self, latitude: float, longitude: float, *, name: str | None = None
            ) -> ResolvedBoundaries:
                probed.append(longitude)
                return super().get_boundaries(latitude, longitude, name=name)

        with mock.patch(_CHAIN, _Recording):
            self.assertEqual(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)), self.old)
        self.assertEqual(probed, [float(self.other.location.longitude)])

    def test_a_nulled_domain_root_still_probes_pins_on_the_parcels_buildings(self) -> None:
        building = make_place(
            PlaceKind.BUILDING, _strip(0.85, 0.95), parent=self.old, relation=PlaceRelation.PART_OF, name="Shed"
        )
        Location.objects.filter(pk=self.other.location_id).update(place=building)
        Place.objects.filter(pk__in=[self.old.pk, building.pk]).update(domain_root=None)
        self.old.refresh_from_db()
        with mock.patch(_CHAIN, _chain_answering(_strip(0.2, 1.0))):
            self.assertEqual(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.2)), self.old)


class RefreshedOutlineTests(_OldParcelCase):
    """The refreshed outline becomes a successor too, so it answers to the same rules when it describes a stored parcel."""

    def test_a_refreshed_outline_describing_a_parcel_with_strangers_pins_takes_nothing_into_the_split(self) -> None:
        theirs = make_place(PlaceKind.PARCEL, _strip(0, 0.6), name="Theirs")
        pin_on(baker.make(User).profile, theirs, lat=SOUTH + SIZE / 2, lng=WEST + SIZE * 0.3)
        remainder = self.remainder_parcel()
        with mock.patch(_CHAIN):
            superseded = provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6))
        self.assertIsNone(superseded)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.CURRENT)
        for place in (theirs, remainder):
            place.refresh_from_db()
            self.assertIsNone(place.parent_id)
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_a_refreshed_outline_describing_a_parcel_of_another_family_takes_nothing_into_the_split(self) -> None:
        site = make_place(PlaceKind.PARCEL, _strip(2, 3), name="Site")
        taken = make_place(
            PlaceKind.PARCEL, _strip(0, 0.6), parent=site, relation=PlaceRelation.MEMBER_OF, name="Taken"
        )
        remainder = self.remainder_parcel()
        with mock.patch(_CHAIN):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6)))
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.CURRENT)
        taken.refresh_from_db()
        self.assertEqual(taken.parent_id, site.pk)
        remainder.refresh_from_db()
        self.assertIsNone(remainder.parent_id)
        self.assertFalse(PlaceAccessGrant.objects.exists())


class ProcessSplitTests(_OldParcelCase):
    """Every successor passes through process_split, so it holds them to the successor rules whoever called it."""

    def _assert_nothing_split(self, superseded: Place | None) -> None:
        self.assertIsNone(superseded)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.CURRENT)
        self.assertFalse(self.old.children.exists())
        self.assertFalse(PlaceAccessGrant.objects.exists())

    def test_an_outline_beside_the_old_parcel_is_no_successor(self) -> None:
        self._assert_nothing_split(process_split(self.old, [_strip(0, 0.6), _strip(1.0, 1.8)]))

    def test_an_outline_overlapping_another_successor_is_no_successor(self) -> None:
        self._assert_nothing_split(process_split(self.old, [_strip(0, 0.6), _strip(0.3, 1.0)]))

    def test_one_outline_offered_twice_is_too_few_successors_to_split(self) -> None:
        self._assert_nothing_split(process_split(self.old, [_strip(0, 0.6), _strip(0, 0.6)]))

    def test_an_outline_offered_twice_becomes_one_child(self) -> None:
        self.assertEqual(process_split(self.old, [_strip(0, 0.6), _strip(0, 0.6), _strip(0.6, 1.0)]), self.old)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.SUPERSEDED)
        self.assertEqual(self.old.children.count(), 2)

    def test_two_outlines_landing_on_one_new_parcel_create_nothing(self) -> None:
        first, second = _each_holding_the_others_centroid()
        places_before = Place.objects.count()

        self._assert_nothing_split(process_split(self.old, [first, second]))

        self.assertEqual(Place.objects.count(), places_before)

    def test_two_outlines_landing_on_one_stored_parcel_leave_it_as_it_was(self) -> None:
        first, second = _each_holding_the_others_centroid()
        # Matches the first outline, which then rewrites it into something the second outline matches too.
        original = MultiPolygon(*_strip(0.35, 0.45), *_strip(0.8, 0.95), srid=4326)
        stored = make_place(PlaceKind.PARCEL, original, name="Stored")

        self._assert_nothing_split(process_split(self.old, [first, second]))

        stored.refresh_from_db()
        self.assertIsNone(stored.parent_id)
        self.assertTrue(stored.geometry.equals(original))

    def test_pieces_of_the_old_parcel_split_it(self) -> None:
        self.assertEqual(process_split(self.old, [_strip(0, 0.6), _strip(0.6, 1.0)]), self.old)
        self.old.refresh_from_db()
        self.assertEqual(self.old.status, PlaceStatus.SUPERSEDED)
        self.assertEqual(self.old.children.count(), 2)


class BrokenGeometryTests(_OldParcelCase):
    def test_a_geos_error_skips_split_detection_instead_of_failing_the_refresh(self) -> None:
        with (
            mock.patch(_CHAIN),
            mock.patch(
                "urbanlens.dashboard.services.places.splits.remainder_of", side_effect=GEOSException("bad ring")
            ),
        ):
            self.assertIsNone(provisioning.detect_subdivision(self.pin.location, _strip(0, 0.6)))
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

    def test_a_predecessor_whose_domain_root_was_nulled_still_has_its_own_holders(self) -> None:
        Place.objects.filter(pk=self.old.pk).update(domain_root=None)
        self.old.refresh_from_db()
        self.assertEqual(holders_of([self.old], timezone.now()), {self.holder.pk})

    def test_repeating_an_event_is_harmless_and_nothing_is_removed(self) -> None:
        event, _ = self._event(LineageEventKind.SPLIT)
        handle_lineage_event(event)
        before = PlaceAccessGrant.objects.count()
        self.pin.delete()
        handle_lineage_event(event)
        self.assertEqual(PlaceAccessGrant.objects.count(), before)
