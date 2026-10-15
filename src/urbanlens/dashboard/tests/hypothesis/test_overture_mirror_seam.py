"""A coordinate an Overture mirror covers is answered by the mirror, never read from Overture itself."""

from __future__ import annotations

from typing import Any, ClassVar
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.points import OVERTURE_MIRRORS
from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
from urbanlens.dashboard.services.apis.locations.boundaries.overture import (
    OvertureMirror,
    OvertureProvider,
    overture_mirror_for,
)
from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError, UpstreamBusyError

_LAT, _LON = 41.7331, -73.9301
_PUBLIC = "urbanlens.dashboard.services.apis.locations.boundaries.overture._public_release"


def _footprint(half: float = 0.0005) -> dict[str, Any]:
    ring = [
        [_LON - half, _LAT - half],
        [_LON + half, _LAT - half],
        [_LON + half, _LAT + half],
        [_LON - half, _LAT + half],
        [_LON - half, _LAT - half],
    ]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {"height": 9.0}}


class _Mirror(OvertureMirror):
    key: ClassVar[str] = "test_mirror"

    def __init__(
        self,
        *,
        covers: bool = True,
        features: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        available: bool = True,
    ) -> None:
        self._covers = covers
        self._features = features or []
        self._error = error
        self._available = available

    def covers(self, latitude: float, longitude: float) -> bool:
        return self._covers

    def building_features(self, latitude: float, longitude: float) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return self._features

    def place_features(self, latitude: float, longitude: float, *, radius_m: float) -> list[dict[str, Any]]:
        return []

    def available(self) -> bool:
        return self._available


class MirrorSelectionTests(SimpleTestCase):
    def test_a_covering_mirror_is_found(self) -> None:
        mirror = _Mirror()
        with OVERTURE_MIRRORS.overridden([_Mirror(covers=False), mirror]):
            self.assertIs(overture_mirror_for(_LAT, _LON), mirror)

    def test_with_no_mirror_covering_the_public_release_serves(self) -> None:
        with OVERTURE_MIRRORS.overridden([_Mirror(covers=False)]):
            self.assertIsNone(overture_mirror_for(_LAT, _LON))
        with OVERTURE_MIRRORS.overridden([]):
            self.assertIsNone(overture_mirror_for(_LAT, _LON))


class ProviderTests(SimpleTestCase):
    def test_a_covered_coordinate_is_answered_by_the_mirror_and_never_reads_overture(self) -> None:
        with OVERTURE_MIRRORS.overridden([_Mirror(features=[_footprint()])]), mock.patch(_PUBLIC) as public:
            polygon = OvertureProvider().get_boundary(_LAT, _LON)

        self.assertIsNotNone(polygon)
        public.assert_not_called()

    def test_a_covered_coordinate_the_mirror_holds_nothing_for_still_never_reads_overture(self) -> None:
        with OVERTURE_MIRRORS.overridden([_Mirror(features=[])]), mock.patch(_PUBLIC) as public:
            self.assertIsNone(OvertureProvider().get_boundary(_LAT, _LON))

        public.assert_not_called()

    def test_an_uncovered_coordinate_reads_the_public_release(self) -> None:
        with OVERTURE_MIRRORS.overridden([_Mirror(covers=False)]), mock.patch(_PUBLIC) as public:
            OvertureProvider().get_boundary(_LAT, _LON)

        public.return_value.get_boundary.assert_called_once_with(_LAT, _LON)

    def test_a_mirror_that_cannot_answer_defers_the_provider(self) -> None:
        with (
            OVERTURE_MIRRORS.overridden([_Mirror(error=ProviderUnavailableError("down"))]),
            self.assertRaises(BoundaryProviderDeferredError) as caught,
        ):
            OvertureProvider().get_boundary(_LAT, _LON)

        self.assertEqual(caught.exception.service_key, "overture")

    def test_the_wait_a_busy_mirror_names_is_passed_on(self) -> None:
        class BusyError(UpstreamBusyError, ProviderUnavailableError):
            pass

        with (
            OVERTURE_MIRRORS.overridden([_Mirror(error=BusyError("busy", retry_after=90))]),
            self.assertRaises(BoundaryProviderDeferredError) as caught,
        ):
            OvertureProvider().get_boundary(_LAT, _LON)

        self.assertEqual(caught.exception.retry_after, 90)

    def test_nearby_places_from_an_unavailable_mirror_are_none_and_not_overtures(self) -> None:
        with OVERTURE_MIRRORS.overridden([_Mirror(available=False)]), mock.patch(_PUBLIC) as public:
            self.assertEqual(OvertureProvider().get_nearby_places(_LAT, _LON), [])

        public.assert_not_called()
