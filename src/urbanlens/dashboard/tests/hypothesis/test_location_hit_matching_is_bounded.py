"""Matching a photo library against a profile's pins ran one PostGIS query per photo.

N21 H01's remaining half. The queue half is fixed (the sweep declares
`Queue.BULK`, so it no longer holds an interactive slot) and the double-press
half is fixed (`single_flight.claim` before the enqueue). What is left is the
cost of the sweep itself.

`_match_hits_to_pins` already prefilters candidate pins with an indexed
`near_point` query and caches the result - but the cache is keyed on the hit's
**exact** float coordinates. Two photos taken standing in the same spot have
different GPS to six decimal places, so the key is effectively unique per photo
and the cache never hits. A 100,000-photo library is 100,000 spatial queries,
each holding the worker's Postgres backend, for a sweep that is one button
press.

The fix snaps the key to a grid and widens the query radius by the cell's
half-diagonal, so every photo in one cell shares a query. That is safe because
the prefilter only has to return a **superset**: `_match_hits_to_pins` then
checks `polygon.contains(point)` against the hit's real coordinates, so a
too-generous candidate list changes nothing about which pin a hit matches. The
tests below assert both halves of that - the query count stops tracking the
photo count, and the matching answers do not move.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.pins.pin_suggestions import LocationHit, _match_hits_to_pins

_PIN_LAT = Decimal("40.000000")
_PIN_LON = Decimal("-74.000000")


def _hit(lat: float, lon: float) -> LocationHit:
    return LocationHit(
        latitude=lat,
        longitude=lon,
        taken_at=datetime.datetime(2024, 1, 1, 12, 0, tzinfo=datetime.UTC),
    )


class _MatchingCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.location = baker.make_recipe("dashboard.location", latitude=_PIN_LAT, longitude=_PIN_LON)
        self.pin = baker.make_recipe("dashboard.pin", profile=self.profile, location=self.location)

    def _queries_for(self, hits: list[LocationHit]) -> int:
        """Every query matching *hits* costs - the prefilter is the part that scales."""
        with CaptureQueriesContext(connection) as captured:
            _match_hits_to_pins(self.profile, hits)
        return len(captured.captured_queries)


class OneQueryPerPhotoTests(_MatchingCase):
    """A library in one place must not cost a query per photo."""

    def _clustered_hits(self, count: int) -> list[LocationHit]:
        """*count* hits a few metres apart, as a burst of photos in one spot is."""
        return [_hit(40.0 + index * 0.000001, -74.0 + index * 0.000001) for index in range(count)]

    def test_the_query_count_does_not_track_the_photo_count(self) -> None:
        few = self._queries_for(self._clustered_hits(5))
        many = self._queries_for(self._clustered_hits(200))

        self.assertLessEqual(
            many,
            few + 2,
            f"matching 200 photos in one spot cost {many} queries against {few} for 5 - the prefilter cache is not being hit",
        )

    def test_photos_in_one_spot_share_a_single_prefilter(self) -> None:
        """Stated absolutely as well as relatively, so a uniformly-bad baseline cannot pass."""
        self.assertLessEqual(self._queries_for(self._clustered_hits(200)), 10)


class TheAnswersDoNotMoveTests(_MatchingCase):
    """The anti-vacuity half: a prefilter that returns nothing would pass the tests above."""

    def test_a_hit_at_the_pin_still_matches_it(self) -> None:
        matched, unmatched = _match_hits_to_pins(self.profile, [_hit(40.0, -74.0)])

        self.assertEqual(list(matched), [self.pin])
        self.assertEqual(unmatched, [])

    def test_a_hit_far_away_still_does_not_match(self) -> None:
        matched, unmatched = _match_hits_to_pins(self.profile, [_hit(41.5, -76.5)])

        self.assertEqual(matched, {})
        self.assertEqual(len(unmatched), 1)

    def test_a_mixed_batch_is_split_the_same_way(self) -> None:
        """Grid snapping must not let a near hit borrow a far hit's candidate list."""
        near, far = _hit(40.0, -74.0), _hit(41.5, -76.5)

        matched, unmatched = _match_hits_to_pins(self.profile, [near, far])

        self.assertEqual(matched.get(self.pin), [near])
        self.assertEqual(unmatched, [far])

    def test_a_hit_just_outside_the_pin_is_still_unmatched(self) -> None:
        """The boundary check runs on the real coordinates, not the snapped ones."""
        matched, unmatched = _match_hits_to_pins(self.profile, [_hit(40.02, -74.02)])

        self.assertEqual(matched, {})
        self.assertEqual(len(unmatched), 1)
