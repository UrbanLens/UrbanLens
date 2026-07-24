"""Tests for boundary voting: recency-weighted community selection of official boundaries."""

from __future__ import annotations

from datetime import timedelta
from itertools import count
from unittest.mock import patch

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.boundary.model import Boundary, BoundarySource, BoundaryType
from urbanlens.dashboard.models.boundary_vote.model import BoundaryVote
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.boundary_voting import (
    HALF_LIFE_DAYS,
    BoundaryVoteError,
    apply_winning_boundary,
    boundary_options,
    boundary_vote_context,
    cast_boundary_vote,
    has_consensus,
    vote_weight,
    winning_boundary,
)
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries, generate_location_boundaries

_coordinate_counter = count()


def _square(lon: float, lat: float, size: float = 0.001) -> MultiPolygon:
    """A small square MultiPolygon with its lower-left corner at (lon, lat)."""
    ring = ((lon, lat), (lon + size, lat), (lon + size, lat + size), (lon, lat + size), (lon, lat))
    return MultiPolygon(Polygon(ring, srid=4326), srid=4326)


def _make_location() -> Location:
    """A Location with coordinates distinct from every other test location."""
    offset = next(_coordinate_counter)
    return baker.make(Location, latitude=f"42.{650_000 + offset}", longitude=f"-73.{760_000 + offset}")


def _make_candidate(location: Location, source: str, polygon: MultiPolygon) -> Boundary:
    """A per-provider source-candidate Boundary row for ``location``."""
    return baker.make(
        Boundary,
        location=location,
        boundary_type=BoundaryType.PROPERTY,
        source=source,
        generated_polygon=polygon,
        generated_at=timezone.now(),
    )


def _make_profiles(count_: int) -> list[Profile]:
    """Create ``count_`` users and return their auto-created profiles."""
    return [Profile.objects.get(user=baker.make("auth.User")) for _ in range(count_)]


def _winner_pk(location: Location) -> int | None:
    """PK of the winning boundary, or None - narrows the Optional for assertions."""
    winner = winning_boundary(location)
    return winner.pk if winner is not None else None


def _backdate(vote: BoundaryVote, days: float) -> None:
    """Push a vote's recency timestamp ``days`` into the past.

    Queryset ``update()`` bypasses ``auto_now``, so this sets ``updated``
    exactly - the field the weighting reads.
    """
    BoundaryVote.objects.filter(pk=vote.pk).update(updated=timezone.now() - timedelta(days=days))


class VoteWeightTests(SimpleTestCase):
    """Pure math: the half-life decay weighting."""

    def test_fresh_vote_has_full_weight(self) -> None:
        now = timezone.now()
        self.assertAlmostEqual(vote_weight(now, now), 1.0)

    def test_weight_halves_at_half_life(self) -> None:
        now = timezone.now()
        self.assertAlmostEqual(vote_weight(now - timedelta(days=HALF_LIFE_DAYS), now), 0.5)

    def test_older_votes_weigh_less(self) -> None:
        now = timezone.now()
        newer = vote_weight(now - timedelta(days=10), now)
        older = vote_weight(now - timedelta(days=40), now)
        self.assertLess(older, newer)

    def test_same_age_votes_tie_exactly(self) -> None:
        now = timezone.now()
        stamp = now - timedelta(days=33)
        self.assertEqual(vote_weight(stamp, now), vote_weight(stamp, now))

    def test_future_timestamp_clamps_to_unit_weight(self) -> None:
        now = timezone.now()
        self.assertEqual(vote_weight(now + timedelta(days=5), now), 1.0)


class BoundaryOptionsTests(TestCase):
    """Which Boundary rows count as votable candidates."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()

    def test_only_externally_sourced_property_candidates_qualify(self) -> None:
        redata = _make_candidate(self.location, BoundarySource.REDATA, _square(-73.76, 42.65))
        overpass = _make_candidate(self.location, BoundarySource.OVERPASS, _square(-73.761, 42.65))
        # The canonical default, a building candidate, and a wiki-drawn row
        # must never be votable - the vote is strictly between official
        # external property datasets, so user drawings can't become the
        # official matching boundary.
        baker.make(Boundary, location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_square(-73.762, 42.65))
        baker.make(Boundary, location=self.location, boundary_type=BoundaryType.BUILDING, source=BoundarySource.OVERPASS, generated_polygon=_square(-73.763, 42.65))
        wiki = baker.make(Wiki, location=self.location)
        baker.make(Boundary, wiki=wiki, location=self.location, boundary_type=BoundaryType.PROPERTY, polygon=_square(-73.764, 42.65))

        options = boundary_options(self.location)
        self.assertEqual([option.pk for option in options], [redata.pk, overpass.pk])

    def test_candidates_without_geometry_are_skipped(self) -> None:
        baker.make(Boundary, location=self.location, boundary_type=BoundaryType.PROPERTY, source=BoundarySource.REDATA, generated_polygon=None)
        self.assertEqual(boundary_options(self.location), [])

    def test_candidates_are_excluded_from_location_defaults(self) -> None:
        _make_candidate(self.location, BoundarySource.OVERPASS, _square(-73.76, 42.65))
        canonical = baker.make(Boundary, location=self.location, boundary_type=BoundaryType.PROPERTY, generated_polygon=_square(-73.761, 42.65))
        defaults = list(Boundary.objects.location_defaults().filter(location=self.location))
        self.assertEqual([row.pk for row in defaults], [canonical.pk])


class WinningBoundaryTests(TestCase):
    """The spec's weighting scenarios, verbatim."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()
        self.redata = _make_candidate(self.location, BoundarySource.REDATA, _square(-73.76, 42.65))
        self.overpass = _make_candidate(self.location, BoundarySource.OVERPASS, _square(-73.761, 42.65))
        self.voters = _make_profiles(3)

    def test_zero_votes_defaults_to_redata(self) -> None:
        self.assertEqual(_winner_pk(self.location), self.redata.pk)

    def test_single_overpass_vote_beats_redata(self) -> None:
        baker.make(BoundaryVote, location=self.location, boundary=self.overpass, profile=self.voters[0])
        self.assertEqual(_winner_pk(self.location), self.overpass.pk)

    def test_newer_redata_vote_outweighs_older_overpass_vote(self) -> None:
        overpass_vote = baker.make(BoundaryVote, location=self.location, boundary=self.overpass, profile=self.voters[0])
        _backdate(overpass_vote, 30)
        baker.make(BoundaryVote, location=self.location, boundary=self.redata, profile=self.voters[1])
        self.assertEqual(_winner_pk(self.location), self.redata.pk)

    def test_exact_same_age_tie_goes_to_redata(self) -> None:
        vote_a = baker.make(BoundaryVote, location=self.location, boundary=self.overpass, profile=self.voters[0])
        vote_b = baker.make(BoundaryVote, location=self.location, boundary=self.redata, profile=self.voters[1])
        stamp = timezone.now() - timedelta(days=7)
        BoundaryVote.objects.filter(pk__in=[vote_a.pk, vote_b.pk]).update(updated=stamp)
        self.assertEqual(_winner_pk(self.location), self.redata.pk)

    def test_no_candidates_means_no_winner(self) -> None:
        empty_location = _make_location()
        self.assertIsNone(winning_boundary(empty_location))

    def test_consensus_rules(self) -> None:
        # No votes: nothing to be settled on.
        self.assertFalse(has_consensus(self.location))
        # Only one candidate has votes: settled.
        vote = baker.make(BoundaryVote, location=self.location, boundary=self.overpass, profile=self.voters[0])
        self.assertTrue(has_consensus(self.location))
        # A same-age 1v1 split is far below the 1.5x ratio: contested again.
        rival = baker.make(BoundaryVote, location=self.location, boundary=self.redata, profile=self.voters[1])
        stamp = timezone.now()
        BoundaryVote.objects.filter(pk__in=[vote.pk, rival.pk]).update(updated=stamp)
        self.assertFalse(has_consensus(self.location))
        # 2v1 at the same age crosses the 1.5x ratio: settled.
        third = baker.make(BoundaryVote, location=self.location, boundary=self.redata, profile=self.voters[2])
        BoundaryVote.objects.filter(pk=third.pk).update(updated=stamp)
        self.assertTrue(has_consensus(self.location))

    def test_consensus_false_with_fewer_than_two_candidates(self) -> None:
        lone_location = _make_location()
        _make_candidate(lone_location, BoundarySource.REDATA, _square(-73.70, 42.60))
        self.assertFalse(has_consensus(lone_location))


class WinnerMatchingIntegrationTests(TestCase):
    """The vote winner must become the geometry matching actually uses."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()
        self.redata_polygon = _square(-73.76, 42.65)
        self.overpass_polygon = _square(-73.90, 42.80)
        self.redata = _make_candidate(self.location, BoundarySource.REDATA, self.redata_polygon)
        self.overpass = _make_candidate(self.location, BoundarySource.OVERPASS, self.overpass_polygon)
        # Canonical row starts as the chain default: REData-first.
        self.canonical = baker.make(
            Boundary,
            location=self.location,
            boundary_type=BoundaryType.PROPERTY,
            generated_polygon=self.redata_polygon,
            generated_at=timezone.now(),
        )
        (self.voter,) = _make_profiles(1)

    def test_casting_a_vote_rewrites_the_canonical_matching_polygon(self) -> None:
        cast_boundary_vote(self.location, self.voter, self.overpass.pk)
        self.canonical.refresh_from_db()
        assert self.canonical.generated_polygon is not None
        self.assertEqual(self.canonical.generated_polygon.wkb, self.overpass_polygon.wkb)
        # And the resolution chain every matcher consults now serves it.
        row = Boundary.objects.row_for_location(self.location, BoundaryType.PROPERTY)
        self.assertEqual(row.pk, self.canonical.pk)
        self.assertEqual(row.generated_polygon.wkb, self.overpass_polygon.wkb)

    def test_effective_wiki_polygon_reflects_the_winner(self) -> None:
        wiki = baker.make(Wiki, location=self.location)
        cast_boundary_vote(self.location, self.voter, self.overpass.pk)
        polygon, source = Boundary.objects.resolve_for_wiki(wiki, BoundaryType.PROPERTY)
        self.assertEqual(source, "generated")
        assert polygon is not None
        self.assertEqual(polygon.wkb, self.overpass_polygon.wkb)

    def test_location_point_matching_uses_the_winner(self) -> None:
        cast_boundary_vote(self.location, self.voter, self.overpass.pk)
        inside_overpass = Point(-73.8995, 42.8005, srid=4326)
        matched = Location.objects.filter(pk=self.location.pk).filter(Location.objects.filter()._boundary_polygon_q(inside_overpass))
        self.assertTrue(matched.exists())
        inside_redata_only = Point(-73.7595, 42.6505, srid=4326)
        unmatched = Location.objects.filter(pk=self.location.pk).filter(Location.objects.filter()._boundary_polygon_q(inside_redata_only))
        self.assertFalse(unmatched.exists())

    def test_apply_without_votes_leaves_the_default_alone(self) -> None:
        self.assertIsNone(apply_winning_boundary(self.location))
        self.canonical.refresh_from_db()
        assert self.canonical.generated_polygon is not None
        self.assertEqual(self.canonical.generated_polygon.wkb, self.redata_polygon.wkb)

    def test_generation_persists_candidates_and_respects_existing_votes(self) -> None:
        location = _make_location()
        redata_polygon = _square(-73.50, 42.50)
        overpass_polygon = _square(-73.51, 42.50)
        resolved = ResolvedBoundaries(
            property_polygon=redata_polygon,
            building_polygon=None,
            property_candidates=[("redata_boundary", redata_polygon), ("overpass", overpass_polygon)],
        )
        with patch("urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries", return_value=resolved):
            generate_location_boundaries(location)

        candidates = boundary_options(location)
        self.assertEqual([candidate.source for candidate in candidates], [BoundarySource.REDATA, BoundarySource.OVERPASS])
        canonical = Boundary.objects.row_for_location(location, BoundaryType.PROPERTY)
        self.assertEqual(canonical.generated_polygon.wkb, redata_polygon.wkb)

        # A vote for overpass, then a regeneration: the vote must survive.
        cast_boundary_vote(location, self.voter, candidates[1].pk)
        with patch("urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries", return_value=resolved):
            generate_location_boundaries(location)
        canonical.refresh_from_db()
        self.assertEqual(canonical.generated_polygon.wkb, overpass_polygon.wkb)


class BoundaryVoteEndpointTests(TestCase):
    """POST /location/<slug>/wiki/boundary/vote/."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()
        self.wiki = baker.make(Wiki, location=self.location)
        self.redata = _make_candidate(self.location, BoundarySource.REDATA, _square(-73.76, 42.65))
        self.overpass = _make_candidate(self.location, BoundarySource.OVERPASS, _square(-73.761, 42.65))
        (self.voter,) = _make_profiles(1)
        baker.make(Pin, profile=self.voter, location=self.location)
        self.client.force_login(self.voter.user)
        self.url = reverse("location.wiki.boundary_vote", args=[self.location.slug])

    def test_vote_and_change_vote_keep_one_row_per_profile(self) -> None:
        response = self.client.post(self.url, {"boundary_id": self.overpass.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["my_vote_id"], self.overpass.pk)
        self.assertEqual(BoundaryVote.objects.for_location(self.location).count(), 1)

        first_updated = BoundaryVote.objects.get(profile=self.voter).updated
        response = self.client.post(self.url, {"boundary_id": self.redata.pk})
        self.assertEqual(response.status_code, 200)
        vote = BoundaryVote.objects.get(profile=self.voter)
        self.assertEqual(vote.boundary_id, self.redata.pk)
        self.assertEqual(BoundaryVote.objects.for_location(self.location).count(), 1)
        self.assertGreaterEqual(vote.updated, first_updated)

    def test_rejects_boundary_from_another_location(self) -> None:
        elsewhere = _make_location()
        foreign = _make_candidate(elsewhere, BoundarySource.REDATA, _square(-73.70, 42.60))
        response = self.client.post(self.url, {"boundary_id": foreign.pk})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(BoundaryVote.objects.count(), 0)

    def test_rejects_missing_boundary_id(self) -> None:
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 400)

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.post(self.url, {"boundary_id": self.overpass.pk})
        self.assertEqual(response.status_code, 302)

    def test_requires_a_pin_at_the_location(self) -> None:
        (outsider,) = _make_profiles(1)
        self.client.force_login(outsider.user)
        response = self.client.post(self.url, {"boundary_id": self.overpass.pk})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(BoundaryVote.objects.count(), 0)


class BoundaryVoteContextTests(TestCase):
    """Dialog gating: auto-open only pre-vote, button context otherwise."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.location = _make_location()
        self.redata = _make_candidate(self.location, BoundarySource.REDATA, _square(-73.76, 42.65))
        self.overpass = _make_candidate(self.location, BoundarySource.OVERPASS, _square(-73.761, 42.65))
        (self.viewer,) = _make_profiles(1)

    def test_no_votes_yields_auto_open(self) -> None:
        context = boundary_vote_context(self.location, self.viewer)
        assert context is not None
        self.assertTrue(context["auto_open"])
        self.assertFalse(context["has_votes"])
        self.assertIsNone(context["my_vote_id"])
        self.assertEqual(len(context["options"]), 2)
        self.assertEqual(context["options"][0]["source"], BoundarySource.REDATA)
        self.assertIsInstance(context["options"][0]["polygon"], dict)

    def test_any_vote_stops_auto_open(self) -> None:
        (other,) = _make_profiles(1)
        cast_boundary_vote(self.location, other, self.overpass.pk)
        context = boundary_vote_context(self.location, self.viewer)
        assert context is not None
        self.assertFalse(context["auto_open"])
        self.assertTrue(context["has_votes"])
        self.assertIsNone(context["my_vote_id"])

    def test_viewers_own_choice_is_marked(self) -> None:
        cast_boundary_vote(self.location, self.viewer, self.overpass.pk)
        context = boundary_vote_context(self.location, self.viewer)
        assert context is not None
        self.assertEqual(context["my_vote_id"], self.overpass.pk)
        marked = {option["id"]: option["is_my_choice"] for option in context["options"]}
        self.assertTrue(marked[self.overpass.pk])
        self.assertFalse(marked[self.redata.pk])

    def test_single_candidate_renders_nothing(self) -> None:
        lone_location = _make_location()
        _make_candidate(lone_location, BoundarySource.REDATA, _square(-73.70, 42.60))
        self.assertIsNone(boundary_vote_context(lone_location, self.viewer))

    def test_invalid_choice_raises(self) -> None:
        with pytest.raises(BoundaryVoteError):
            cast_boundary_vote(self.location, self.viewer, 999_999)
