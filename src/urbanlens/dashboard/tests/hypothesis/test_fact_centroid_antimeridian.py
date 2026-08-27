"""Averaging POINT evidence must survive the antimeridian.

``_aggregate_point`` averaged longitude arithmetically. Longitude wraps, so two
observations of one place either side of the date line - 179.99 and -179.99 -
averaged to 0.0: a centroid in the Atlantic, some 20,000km from either
observation, which ``recompute`` then stored as the fact's value.

Confidence collapsed to 0 in that case (both observations are nowhere near the
bogus centroid), which limited the damage but did not prevent the wrong point
being written. Worse, two observers who genuinely *agreed* were scored as
disagreeing, so a fact near the date line could never be confirmed at all.

The fix averages the unit vectors and takes the angle back, which returns the
same answer as the arithmetic mean everywhere else on Earth - the ordinary case
is asserted here too, because a "fix" that shifted every other centroid would be
a far bigger bug than the one it corrected.
"""

from __future__ import annotations

from django.contrib.gis.geos import Point

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.facts.confidence import _aggregate_point, _WeightedEvidence


def _at(longitude: float, latitude: float, weight: float = 1.0) -> _WeightedEvidence:
    return _WeightedEvidence(value=Point(longitude, latitude, srid=4326), weight=weight)


class FactCentroidAntimeridianTests(SimpleTestCase):
    def test_an_ordinary_centroid_is_unchanged(self) -> None:
        """The fix must not move any of the longitudes that already worked."""
        centroid, _confidence = _aggregate_point([_at(-71.45, 41.35), _at(-71.4501, 41.3501)])

        self.assertAlmostEqual(centroid.x, -71.45005, places=4)
        self.assertAlmostEqual(centroid.y, 41.35005, places=4)

    def test_the_centroid_of_two_points_across_the_date_line_sits_between_them(self) -> None:
        centroid, _confidence = _aggregate_point([_at(179.99, -16.5), _at(-179.99, -16.5)])

        self.assertAlmostEqual(abs(centroid.x), 180.0, places=3, msg=f"centroid landed at longitude {centroid.x}")

    def test_observers_who_agree_across_the_date_line_are_scored_as_agreeing(self) -> None:
        """The user-visible half: such a fact could never reach confidence at all."""
        # ~10m apart at this latitude, inside AGREEMENT_DISTANCE_METERS.
        centroid, confidence = _aggregate_point([_at(179.99995, -16.5), _at(-179.99995, -16.5)])

        self.assertGreater(confidence, 0.0, f"agreeing observations still scored 0 (centroid at {centroid.x})")

    def test_a_weighted_average_still_leans_toward_the_heavier_side(self) -> None:
        centroid, _confidence = _aggregate_point([_at(179.0, 10.0, weight=3.0), _at(-179.0, 10.0, weight=1.0)])

        self.assertGreater(centroid.x, 179.0, "the heavier observation should pull the centroid toward it")
        self.assertLessEqual(centroid.x, 180.0)

    def test_a_single_observation_is_returned_as_is(self) -> None:
        centroid, _confidence = _aggregate_point([_at(-179.5, 51.9)])

        self.assertAlmostEqual(centroid.x, -179.5, places=6)

    def test_antipodal_observations_do_not_invent_a_midpoint(self) -> None:
        """Exactly opposite longitudes cancel, so there is no meaningful middle.

        The fallback returns one of the *observed* longitudes rather than a
        made-up one. That observation then legitimately agrees with the centroid,
        so confidence is low rather than zero - which is the honest reading of
        "half the evidence supports this point", not a bug.
        """
        centroid, confidence = _aggregate_point([_at(0.0, 0.0), _at(180.0, 0.0)])

        self.assertIn(centroid.x, (0.0, 180.0, -180.0), "the fallback invented a longitude nobody observed")
        self.assertLess(confidence, 0.5, "antipodal evidence must not read as consensus")

    def test_no_evidence_yields_nothing(self) -> None:
        value, confidence = _aggregate_point([])

        self.assertIsNone(value)
        self.assertEqual(confidence, 0.0)
