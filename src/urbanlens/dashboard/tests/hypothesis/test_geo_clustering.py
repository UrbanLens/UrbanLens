"""The densest-cluster search must agree with the pairwise scan it replaced."""

from __future__ import annotations

import random

import pytest

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.geo.clustering import densest_cluster_centroid
from urbanlens.dashboard.services.geo.distance import haversine_km
from urbanlens.dashboard.services.geo.longitude import circular_mean_longitude

RADIUS_KM = 1_000.0

#: How far apart the two implementations may land and still count as the same
#: answer. They are normally identical to floating-point noise; this is slack
#: for the arithmetic, not for the algorithm.
AGREEMENT_KM = 1.0


def reference_centroid(points: list[tuple[float, float]], radius_km: float = RADIUS_KM) -> tuple[float, float]:
    """The pairwise scan, kept as the definition of the right answer.

    Args:
        points: ``(latitude, longitude)`` pairs.
        radius_km: Cluster radius.

    Returns:
        The densest cluster's centroid."""
    seed = max(
        points,
        key=lambda point: sum(
            1 for other in points if haversine_km(point[0], point[1], other[0], other[1]) <= radius_km
        ),
    )
    cluster = [p for p in points if haversine_km(seed[0], seed[1], p[0], p[1]) <= radius_km]
    return sum(p[0] for p in cluster) / len(cluster), circular_mean_longitude([p[1] for p in cluster])


def blob(rng: random.Random, count: int, latitude: float, longitude: float, spread: float) -> list[tuple[float, float]]:
    """A loose scatter of points around one place.

    Args:
        rng: Seeded source of randomness, so failures reproduce.
        count: How many points.
        latitude: Centre latitude in degrees.
        longitude: Centre longitude in degrees.
        spread: Half-width of the scatter in degrees.

    Returns:
        The generated points."""
    return [(latitude + rng.uniform(-spread, spread), longitude + rng.uniform(-spread, spread)) for _ in range(count)]


#: Point sets with an unambiguous densest region, named by what makes each awkward.
AGREEING_CASES = {
    "one tight collection": lambda rng: blob(rng, 200, 42.6, -73.7, 0.5),
    "a lopsided pair of continents": lambda rng: blob(rng, 60, 42.6, -73.7, 2) + blob(rng, 40, 51.5, -0.1, 2),
    "a cluster straddling the antimeridian": lambda rng: (
        blob(rng, 40, -17.0, 179.5, 0.8) + blob(rng, 40, -17.0, -179.5, 0.8)
    ),
    "a polar cluster against an equatorial one": lambda rng: blob(rng, 60, 84.0, 0.0, 3) + blob(rng, 20, 10.0, 10.0, 2),
    "three clusters where the two losers tie": lambda rng: (
        blob(rng, 40, 0.0, 0.0, 1) + blob(rng, 40, 0.0, 60.0, 1) + blob(rng, 10, 40.0, -100.0, 1)
    ),
    "one outlier per twenty pins": lambda rng: blob(rng, 190, 42.6, -73.7, 2) + blob(rng, 10, -30.0, 140.0, 20),
    "every pin in the same place": lambda rng: [(42.6, -73.7)] * 50,
}

#: Point sets with no densest region, where any answer is as good as any other.
TIED_CASES = {
    "two clusters of identical size": lambda rng: blob(rng, 50, 42.6, -73.7, 2) + blob(rng, 50, 51.5, -0.1, 2),
    "two lone pins on different continents": lambda rng: [(40.7, -74.0), (51.5, -0.1)],
    "an evenly spaced line 3,000km long": lambda rng: [(40.0, -120.0 + i * 0.35) for i in range(100)],
    "points scattered over the whole globe": lambda rng: [
        (rng.uniform(-85, 85), rng.uniform(-180, 180)) for _ in range(300)
    ],
}


class ItAgreesWithThePairwiseScanTests(SimpleTestCase):
    """Same answer, wherever there is one answer to have."""

    def test_every_case_with_a_densest_region_agrees(self) -> None:
        for name, source in sorted(AGREEING_CASES.items()):
            with self.subTest(case=name):
                points = source(random.Random(20260910))

                measured = densest_cluster_centroid(points, RADIUS_KM)
                expected = reference_centroid(points)

                assert measured is not None
                gap = haversine_km(measured[0], measured[1], expected[0], expected[1])
                assert gap <= AGREEMENT_KM, f"{measured} is {gap:.1f}km from the reference {expected}"


class ItAnswersTiesTheSameWayEveryTimeTests(SimpleTestCase):
    """Order independence, which is the property the reference lacked."""

    def test_shuffling_the_points_does_not_move_the_centre(self) -> None:
        for name, source in sorted((TIED_CASES | AGREEING_CASES).items()):
            with self.subTest(case=name):
                points = source(random.Random(20260910))
                shuffled = list(points)
                random.Random(11).shuffle(shuffled)

                first = densest_cluster_centroid(points, RADIUS_KM)
                second = densest_cluster_centroid(shuffled, RADIUS_KM)

                assert first is not None and second is not None
                gap = haversine_km(first[0], first[1], second[0], second[1])
                assert gap <= AGREEMENT_KM, f"reordering the same points moved the centre {gap:.1f}km"

    def test_a_tie_still_lands_on_a_real_concentration(self) -> None:
        """Whichever side wins, the centre must not be the midpoint between them."""
        for name, source in sorted(TIED_CASES.items()):
            with self.subTest(case=name):
                points = source(random.Random(20260910))

                measured = densest_cluster_centroid(points, RADIUS_KM)

                assert measured is not None
                nearest = min(haversine_km(measured[0], measured[1], p[0], p[1]) for p in points)
                assert nearest <= RADIUS_KM, f"the centre is {nearest:.0f}km from the nearest pin"


class ItHandlesTheDegenerateInputsTests(SimpleTestCase):
    """The shapes a profile can genuinely be in."""

    def test_no_points_has_no_centre(self) -> None:
        assert densest_cluster_centroid([], RADIUS_KM) is None

    def test_one_point_is_its_own_centre(self) -> None:
        assert densest_cluster_centroid([(42.6, -73.7)], RADIUS_KM) == (42.6, -73.7)

    def test_a_radius_of_zero_is_refused(self) -> None:
        with pytest.raises(ValueError, match="radius_km"):
            densest_cluster_centroid([(1.0, 2.0)], 0.0)

    def test_a_radius_past_the_far_side_of_the_planet_still_answers(self) -> None:
        """Everything is then one cluster, so the answer is every point's centroid."""
        points = [(0.0, 0.0), (0.0, 90.0), (0.0, -90.0)]

        measured = densest_cluster_centroid(points, 40_000.0)

        assert measured is not None
        assert measured[0] == pytest.approx(0.0, abs=1e-6)


class ItCostsAFixedAmountPerPointTests(SimpleTestCase):
    """The point of the rewrite, measured where it is cheap to measure."""

    @given(
        latitude=st.floats(min_value=-70.0, max_value=70.0, allow_nan=False, allow_infinity=False),
        longitude=st.floats(min_value=-179.0, max_value=179.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=25, deadline=None)
    def test_a_single_tight_cluster_centres_on_itself_anywhere_on_the_planet(
        self, latitude: float, longitude: float
    ) -> None:
        points = blob(random.Random(3), 40, latitude, longitude, 0.4)

        measured = densest_cluster_centroid(points, RADIUS_KM)

        assert measured is not None
        expected = reference_centroid(points)
        assert haversine_km(measured[0], measured[1], expected[0], expected[1]) <= AGREEMENT_KM
