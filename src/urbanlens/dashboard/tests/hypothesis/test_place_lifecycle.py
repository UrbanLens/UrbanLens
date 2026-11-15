"""Place lifecycle (ADR34 section 1): keyed matching, geometry versions, lineage edges and version-driven split detection."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.lifecycle import (
    LineageEdgeKind,
    PlaceAccessGrantSource,
    PlaceGeometryVersion,
    PlaceLineageEdge,
)
from urbanlens.dashboard.models.place.model import (
    MAX_PLAUSIBLE_AREA_SQM,
    GrantReason,
    Place,
    PlaceAccessGrant,
    PlaceKind,
    PlaceRelation,
    PlaceStatus,
)
from urbanlens.dashboard.services.places import geometry_versions, lineage
from urbanlens.dashboard.services.places.lineage_events import (
    LineageEvent,
    LineageEventKind,
    handle_lineage_event,
    retract_erroneous_event,
)
from urbanlens.dashboard.services.places.oversized import detach_oversized_place
from urbanlens.dashboard.services.places.provisioning import find_matching_place, upsert_place

from .test_places_access_predicate import pin_on


def rect(lng_min: float, lng_max: float, lat_min: float = 39.997, lat_max: float = 40.003) -> MultiPolygon:
    """An axis-aligned rectangle."""
    ring = ((lng_min, lat_min), (lng_max, lat_min), (lng_max, lat_max), (lng_min, lat_max), (lng_min, lat_min))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


class KeyedMatchingTests(TestCase):
    """Keyed records never merge with each other; a keyless record is absorbed into a keyed one."""

    def test_two_keyed_buildings_with_overlapping_geometry_stay_apart(self) -> None:
        first = upsert_place(PlaceKind.BUILDING, rect(-74.001, -73.999), provider="overture", provider_key="a")
        second = upsert_place(PlaceKind.BUILDING, rect(-74.0011, -73.9991), provider="osm", provider_key="b")
        assert first is not None
        assert second is not None
        assert first.pk != second.pk

    def test_a_keyless_record_is_absorbed_into_a_keyed_place(self) -> None:
        keyed = upsert_place(PlaceKind.BUILDING, rect(-74.001, -73.999), provider="overture", provider_key="a")
        assert keyed is not None
        assert find_matching_place(PlaceKind.BUILDING, rect(-74.0009, -73.9989)) == keyed

    def test_a_keyed_record_adopts_a_keyless_place(self) -> None:
        keyless = upsert_place(PlaceKind.BUILDING, rect(-74.001, -73.999))
        keyed = upsert_place(PlaceKind.BUILDING, rect(-74.0009, -73.9989), provider="overture", provider_key="a")
        assert keyless is not None
        assert keyed is not None
        assert keyed.pk == keyless.pk
        assert keyed.provider_key == "a"

    def test_keyless_against_keyless_still_matches_on_centroids(self) -> None:
        first = upsert_place(PlaceKind.PARCEL, rect(-74.001, -73.999))
        second = upsert_place(PlaceKind.PARCEL, rect(-74.0009, -73.9989))
        assert first is not None
        assert second is not None
        assert first.pk == second.pk


class GeometryVersionTests(TestCase):
    """Geometry is appended, never overwritten in place."""

    def test_upsert_records_each_distinct_outline(self) -> None:
        place = upsert_place(PlaceKind.PARCEL, rect(-74.002, -73.998))
        assert place is not None
        assert place.valid_from is not None
        upsert_place(PlaceKind.PARCEL, rect(-74.002, -73.998))
        assert place.geometry_versions.count() == 1
        upsert_place(PlaceKind.PARCEL, rect(-74.0015, -73.998))
        place.refresh_from_db()
        assert place.geometry_versions.count() == 2
        assert place.geometry.equals(rect(-74.0015, -73.998))

    def test_a_stored_version_cannot_be_rewritten(self) -> None:
        place = upsert_place(PlaceKind.PARCEL, rect(-74.002, -73.998))
        assert place is not None
        version = place.geometry_versions.get()
        version.erroneous = True
        with self.assertRaises(ValueError):
            version.save()

    def test_a_correction_flags_the_wrong_outline_and_keeps_it(self) -> None:
        place = upsert_place(PlaceKind.PARCEL, rect(-74.002, -73.998))
        assert place is not None
        wrong = place.geometry_versions.get()
        geometry_versions.correct_geometry(place, rect(-74.0021, -73.998), erroneous=[wrong])
        wrong.refresh_from_db()
        assert wrong.erroneous
        assert PlaceGeometryVersion.objects.filter(place=place).count() == 2

    def test_a_versions_provider_is_who_drew_it_not_the_places_key_namespace(self) -> None:
        place = Place.objects.create(
            kind=PlaceKind.PARCEL, geometry=rect(-74.002, -73.998), provider="assessor", provider_key="k"
        )
        geometry_versions.set_geometry(place, rect(-74.0015, -73.998), provider="county")
        geometry_versions.set_geometry(place, rect(-74.0012, -73.998))
        providers = list(place.geometry_versions.order_by("recorded_at", "pk").values_list("provider", flat=True))
        # The outline stored before any version has an unknown source; an unnamed refresh is the provider chain's.
        assert providers == ["", "county", ""]
        assert geometry_versions.outline_source(place) == ""


class LineageEdgeTests(TestCase):
    """The handler writes the edges with the snapshot, and only erroneous corrections remove access."""

    def setUp(self) -> None:
        super().setUp()
        self.holder = baker.make(User).profile
        self.old = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996))
        self.new_a = upsert_place(PlaceKind.PARCEL, rect(-74.004, -74.0), provider="x", provider_key="a")
        self.new_b = upsert_place(PlaceKind.PARCEL, rect(-74.0, -73.996), provider="x", provider_key="b")
        assert self.old is not None
        pin_on(self.holder, self.old, lat=40.0, lng=-74.002)

    def _event(self, kind: LineageEventKind):
        from django.utils import timezone

        return LineageEvent(kind, [self.old], [self.new_a, self.new_b], timezone.now())

    def test_a_split_writes_edges_and_closes_validity(self) -> None:
        handle_lineage_event(self._event(LineageEventKind.SPLIT))
        assert PlaceLineageEdge.objects.filter(predecessor=self.old, kind=LineageEdgeKind.SPLIT_INTO).count() == 2
        self.old.refresh_from_db()
        assert self.old.valid_to is not None

    def test_edge_kind_follows_event_kind(self) -> None:
        handle_lineage_event(
            LineageEvent(
                LineageEventKind.MERGE, [self.old], [self.new_a], self._event(LineageEventKind.MERGE).learned_at
            )
        )
        handle_lineage_event(
            LineageEvent(
                LineageEventKind.REPLACE, [self.new_a], [self.new_b], self._event(LineageEventKind.MERGE).learned_at
            )
        )
        assert PlaceLineageEdge.objects.filter(kind=LineageEdgeKind.MERGED_INTO, successor=self.new_a).exists()
        assert PlaceLineageEdge.objects.filter(kind=LineageEdgeKind.REPLACED_BY, successor=self.new_b).exists()

    def test_replaying_an_event_adds_no_duplicate_edges(self) -> None:
        event = self._event(LineageEventKind.SPLIT)
        handle_lineage_event(event)
        handle_lineage_event(event)
        assert PlaceLineageEdge.objects.count() == 2

    def test_an_erroneous_retraction_removes_only_unprovable_grants_and_logs_them(self) -> None:
        handle_lineage_event(self._event(LineageEventKind.SPLIT))
        assert PlaceAccessGrant.objects.filter(
            profile=self.holder, place=self.new_a, reason=GrantReason.GRANDFATHERED_SPLIT
        ).exists()
        with self.assertLogs("urbanlens.dashboard.services.places.lineage_events", level="WARNING") as logs:
            removed = retract_erroneous_event(list(PlaceLineageEdge.objects.all()), "test")
        # One grant each on the predecessor, new_a and new_b, all justified only by the retracted edges.
        assert removed == 3
        assert not PlaceAccessGrant.objects.filter(profile=self.holder).exists()
        assert not PlaceAccessGrant.objects.filter(profile=self.holder, place=self.new_a).exists()
        assert any(str((self.holder.pk, self.new_a.pk)) in line for line in logs.output)
        assert PlaceLineageEdge.objects.filter(erroneous=True).count() == 2

    def test_a_sound_earlier_grant_survives_a_later_erroneous_event_into_the_same_successor(self) -> None:
        handle_lineage_event(self._event(LineageEventKind.SPLIT))
        grant = PlaceAccessGrant.objects.get(profile=self.holder, place=self.new_a)
        other = upsert_place(PlaceKind.PARCEL, rect(-73.99, -73.98), provider="x", provider_key="c")
        assert other is not None
        pin_on(self.holder, other, lat=40.0, lng=-73.985)
        # Learned after that pin, or the event would find no holders and record no source to retract.
        later = LineageEvent(
            LineageEventKind.SPLIT, [other], [self.new_a], self._event(LineageEventKind.SPLIT).learned_at
        )
        handle_lineage_event(later)
        assert PlaceAccessGrantSource.objects.filter(grant=grant).count() == 2
        on_other = PlaceAccessGrant.objects.get(profile=self.holder, place=other)
        bad = list(PlaceLineageEdge.objects.filter(predecessor=other))
        # The grant on other rests only on the bad edge; the one on new_a also rests on the sound split.
        assert retract_erroneous_event(bad, "test") == 1
        assert not PlaceAccessGrant.objects.filter(pk=on_other.pk).exists()
        assert PlaceAccessGrant.objects.filter(pk=grant.pk).exists()

    def test_a_grant_justified_by_a_sound_and_an_erroneous_edge_survives(self) -> None:
        handle_lineage_event(self._event(LineageEventKind.SPLIT))
        grant = PlaceAccessGrant.objects.get(profile=self.holder, place=self.new_a)
        replay = LineageEvent(
            LineageEventKind.REPLACE, [self.old], [self.new_a], self._event(LineageEventKind.SPLIT).learned_at
        )
        handle_lineage_event(replay)
        assert PlaceAccessGrantSource.objects.filter(grant=grant).count() == 2
        bad = list(PlaceLineageEdge.objects.filter(kind=LineageEdgeKind.REPLACED_BY))
        assert retract_erroneous_event(bad, "test") == 0
        assert PlaceAccessGrant.objects.filter(pk=grant.pk).exists()

    def test_a_grant_with_no_sources_is_never_retracted(self) -> None:
        legacy = PlaceAccessGrant.objects.create(
            profile=self.holder, place=self.new_a, reason=GrantReason.GRANDFATHERED_SPLIT
        )
        handle_lineage_event(self._event(LineageEventKind.SPLIT))
        # The sourced grants on the predecessor and new_b go; the legacy grant on new_a stays.
        assert retract_erroneous_event(list(PlaceLineageEdge.objects.all()), "test") == 2
        assert list(PlaceAccessGrant.objects.filter(profile=self.holder).values_list("pk", flat=True)) == [legacy.pk]

    def test_retraction_reopens_the_predecessor(self) -> None:
        handle_lineage_event(self._event(LineageEventKind.SPLIT))
        retract_erroneous_event(list(PlaceLineageEdge.objects.all()), "test")
        self.old.refresh_from_db()
        assert self.old.valid_to is None

    def test_a_relearned_event_retracted_again_reopens_the_predecessor(self) -> None:
        from datetime import timedelta

        first = self._event(LineageEventKind.SPLIT)
        handle_lineage_event(first)
        retract_erroneous_event(list(PlaceLineageEdge.objects.all()), "test")
        later = LineageEvent(first.kind, first.predecessors, first.successors, first.learned_at + timedelta(hours=1))
        handle_lineage_event(later)
        self.old.refresh_from_db()
        assert self.old.valid_to == later.learned_at
        retract_erroneous_event(list(PlaceLineageEdge.objects.all()), "test")
        self.old.refresh_from_db()
        assert self.old.valid_to is None

    def test_retraction_leaves_a_place_later_detached_as_oversized_retired(self) -> None:
        event = self._event(LineageEventKind.SPLIT)
        handle_lineage_event(event)
        Place.objects.filter(pk=self.old.pk).update(area_sqm=MAX_PLAUSIBLE_AREA_SQM[PlaceKind.PARCEL] + 1)
        self.old.refresh_from_db()
        detach_oversized_place(self.old)
        self.old.refresh_from_db()
        assert self.old.status == PlaceStatus.SUPERSEDED
        assert self.old.valid_to == event.learned_at

        retract_erroneous_event(list(PlaceLineageEdge.objects.all()), "test")

        self.old.refresh_from_db()
        assert self.old.status == PlaceStatus.SUPERSEDED
        assert self.old.valid_to == event.learned_at

    def test_events_retracted_one_at_a_time_reopen_the_predecessor_and_free_every_successor(self) -> None:
        from datetime import timedelta

        first = self._event(LineageEventKind.SPLIT)
        handle_lineage_event(first)
        lineage.set_parent(self.new_a, self.old, PlaceRelation.MEMBER_OF)
        closing = list(PlaceLineageEdge.objects.filter(predecessor=self.old))
        handle_lineage_event(
            LineageEvent(LineageEventKind.REPLACE, [self.old], [self.new_b], first.learned_at + timedelta(hours=1))
        )
        later = list(PlaceLineageEdge.objects.filter(kind=LineageEdgeKind.REPLACED_BY))

        retract_erroneous_event(closing, "test")
        self.old.refresh_from_db()
        assert self.old.valid_to == first.learned_at

        retract_erroneous_event(later, "test")
        self.old.refresh_from_db()
        self.new_a.refresh_from_db()
        assert self.old.valid_to is None
        assert self.old.status == PlaceStatus.CURRENT
        assert self.new_a.parent_id is None

    def test_a_same_domain_merge_writes_the_edge_and_no_grants(self) -> None:
        sibling = upsert_place(PlaceKind.PARCEL, rect(-74.004, -74.0005), provider="y", provider_key="s")
        assert sibling is not None
        Place.objects.filter(pk=sibling.pk).update(domain_root=self.old.domain_root_id)
        sibling.refresh_from_db()
        handle_lineage_event(
            LineageEvent(LineageEventKind.MERGE, [sibling], [self.old], self._event(LineageEventKind.MERGE).learned_at)
        )
        assert PlaceLineageEdge.objects.filter(kind=LineageEdgeKind.MERGED_INTO, predecessor=sibling).exists()
        assert not PlaceAccessGrant.objects.exists()

    def test_a_cross_domain_merge_grants_the_holders(self) -> None:
        handle_lineage_event(
            LineageEvent(
                LineageEventKind.MERGE, [self.old], [self.new_a], self._event(LineageEventKind.MERGE).learned_at
            )
        )
        assert PlaceAccessGrant.objects.filter(
            profile=self.holder, place=self.new_a, reason=GrantReason.GRANDFATHERED_LINEAGE
        ).exists()


class VersionSplitDetectionTests(TestCase):
    """A mild shrink whose remainder parcel arrives later is found from the older outline."""

    def test_a_vote_polygon_does_not_trigger_a_split(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="vote")
        upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county")
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

    def test_an_outline_older_than_the_window_is_ignored(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        wider = geometry_versions.latest_version(original)
        assert wider is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        assert (
            PlaceGeometryVersion.objects.filter(pk=wider.pk).update(recorded_at=timezone.now() - timedelta(days=3650))
            == 1
        )
        # The recent narrow outline touches the remainder, so the parcel is still examined; only the aged wider outline could show it shrank.
        assert self._candidates(rect(-73.998, -73.996)) == {original.pk}
        upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county")
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

    def test_a_late_remainder_parcel_completes_the_split(self) -> None:
        from urbanlens.dashboard.services.places import splits

        holder = baker.make(User).profile
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county", provider_key="")
        assert original is not None
        pin_on(holder, original, lat=40.0, lng=-74.003)
        outline = rect(-74.004, -73.998)
        ratio = splits.shrink_ratio(original, outline)
        assert ratio is not None
        self.assertAlmostEqual(ratio, 0.75, places=6)
        # Between the heavy threshold and the keep ceiling, so the refresh alone is not a split.
        assert splits.is_mild_shrink(original, outline)
        shrunk = geometry_versions.set_geometry(original, outline, provider="county")
        assert shrunk
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

        remainder = upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county")
        assert remainder is not None
        original.refresh_from_db()
        assert original.status == PlaceStatus.SUPERSEDED
        assert PlaceLineageEdge.objects.filter(predecessor=original, kind=LineageEdgeKind.SPLIT_INTO).count() == 2
        assert PlaceAccessGrant.objects.filter(
            profile=holder, place=remainder, reason=GrantReason.GRANDFATHERED_SPLIT
        ).exists()

    def test_an_unrelated_neighbour_does_not_split_anything(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        far = upsert_place(PlaceKind.PARCEL, rect(-73.9, -73.89), provider="county")
        assert far is not None
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

    def test_a_boundary_nudge_is_not_a_split(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.9965), provider="county")
        upsert_place(PlaceKind.PARCEL, rect(-73.9965, -73.996), provider="county")
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

    def test_a_split_that_yields_one_successor_is_not_reported(self) -> None:
        from unittest import mock

        from urbanlens.dashboard.services.places import splits

        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        remainder = upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county", detect_splits=False)
        assert remainder is not None
        with mock.patch.object(splits, "process_split", side_effect=lambda place, successors, **_: place) as process:
            assert splits.detect_split_from_versions(remainder) is None
        process.assert_called_once()
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

    def test_a_parcel_keyed_elsewhere_splits_when_its_outlines_came_from_the_remainders_provider(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        Place.objects.filter(pk=original.pk).update(provider="")
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county")
        original.refresh_from_db()
        assert original.status == PlaceStatus.SUPERSEDED

    def test_a_remainder_from_another_provider_is_not_a_version_split(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="osm")
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT

    def test_a_split_successor_keeps_the_provider_and_can_split_again(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county")
        original.refresh_from_db()
        assert original.status == PlaceStatus.SUPERSEDED
        piece = Place.objects.get(parent=original, geometry__equals=rect(-74.004, -73.998))
        assert geometry_versions.outline_source(piece) == "county"

        # A piece matched onto an existing place keeps that place's key namespace; its outlines decide.
        Place.objects.filter(pk=piece.pk).update(provider="")
        geometry_versions.set_geometry(piece, rect(-74.004, -74.0005), provider="county")
        upsert_place(PlaceKind.PARCEL, rect(-74.0005, -73.998), provider="county")
        piece.refresh_from_db()
        assert piece.status == PlaceStatus.SUPERSEDED
        assert PlaceLineageEdge.objects.filter(predecessor=piece, kind=LineageEdgeKind.SPLIT_INTO).count() == 2

    def _candidates(self, new_geometry: MultiPolygon, source: str = "county") -> set[int]:
        from datetime import timedelta

        from django.utils import timezone

        from urbanlens.dashboard.services.places import splits

        horizon = timezone.now() - timedelta(days=30)
        return set(splits._version_split_candidates(new_geometry, source, horizon).values_list("pk", flat=True))

    def test_candidate_needs_an_outline_from_the_new_parcels_provider_that_reaches_it(self) -> None:
        reaches = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert reaches is not None
        # This parcel's county outline is elsewhere; only another provider's outline reaches the new parcel.
        other_drew_it = upsert_place(PlaceKind.PARCEL, rect(-73.9, -73.89), provider="county")
        assert other_drew_it is not None
        geometry_versions.set_geometry(other_drew_it, rect(-74.004, -73.996), provider="osm")
        assert self._candidates(rect(-73.998, -73.996)) == {reaches.pk}

    def test_an_erroneous_outline_does_not_make_a_candidate(self) -> None:
        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        reaching = geometry_versions.latest_version(original)
        assert reaching is not None
        geometry_versions.mark_erroneous([reaching])
        # A sound county outline elsewhere must not lend its row to the erroneous one that reaches the new parcel.
        geometry_versions.set_geometry(original, rect(-73.9, -73.89), provider="county")
        assert self._candidates(rect(-73.998, -73.996)) == set()

    def test_an_outline_past_the_window_does_not_make_a_candidate(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        assert (
            PlaceGeometryVersion.objects.filter(place=original).update(
                recorded_at=timezone.now() - timedelta(days=3650)
            )
            == 1
        )
        # A recent county outline elsewhere must not lend its date to the old one that reaches the new parcel.
        geometry_versions.set_geometry(original, rect(-73.9, -73.89), provider="county")
        assert self._candidates(rect(-73.998, -73.996)) == set()

    def test_an_invalid_outline_skips_the_candidate_instead_of_failing(self) -> None:
        from unittest import mock

        from django.contrib.gis.geos import GEOSException, GEOSGeometry

        from urbanlens.dashboard.services.places import splits

        original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert original is not None
        geometry_versions.set_geometry(original, rect(-74.004, -73.998), provider="county")
        remainder = upsert_place(PlaceKind.PARCEL, rect(-73.998, -73.996), provider="county", detect_splits=False)
        assert remainder is not None
        with mock.patch.object(GEOSGeometry, "intersection", side_effect=GEOSException("TopologyException")) as patched:
            assert splits.detect_split_from_versions(remainder) is None
        patched.assert_called()
        original.refresh_from_db()
        assert original.status == PlaceStatus.CURRENT


class FailedSplitLeavesNoTraceTests(TestCase):
    """A split with fewer than two usable successors undoes everything it wrote while trying."""

    def setUp(self) -> None:
        super().setUp()
        self.holder = baker.make(User).profile
        self.outsider = baker.make(User).profile
        self.original = upsert_place(PlaceKind.PARCEL, rect(-74.004, -73.996), provider="county")
        assert self.original is not None
        pin_on(self.holder, self.original, lat=40.0, lng=-74.003)
        # The east half is already a parcel someone else pinned, so it can never be handed to the holder.
        self.pinned = upsert_place(PlaceKind.PARCEL, rect(-74.0, -73.996), provider="county", detect_splits=False)
        assert self.pinned is not None
        pin_on(self.outsider, self.pinned, lat=40.0, lng=-73.998)

    def test_a_successor_created_for_a_failed_split_is_removed(self) -> None:
        from urbanlens.dashboard.services.places.splits import process_split

        before = set(Place.objects.values_list("pk", flat=True))
        result = process_split(self.original, [rect(-74.004, -74.0), rect(-74.0, -73.996)], source="county")
        assert result.status == PlaceStatus.CURRENT
        assert set(Place.objects.values_list("pk", flat=True)) == before
        assert not PlaceLineageEdge.objects.exists()

    def test_a_matched_successor_keeps_its_outline_and_parent_after_a_failed_split(self) -> None:
        from urbanlens.dashboard.services.places.splits import process_split

        west = upsert_place(PlaceKind.PARCEL, rect(-74.004, -74.0), provider="county", detect_splits=False)
        assert west is not None
        versions = west.geometry_versions.count()
        process_split(self.original, [rect(-74.0041, -74.0), rect(-74.0, -73.996)], source="county")
        west.refresh_from_db()
        assert west.geometry.equals(rect(-74.004, -74.0))
        assert west.geometry_versions.count() == versions
        assert west.parent_id is None
        self.original.refresh_from_db()
        assert self.original.status == PlaceStatus.CURRENT
        assert not self.original.is_aggregate
