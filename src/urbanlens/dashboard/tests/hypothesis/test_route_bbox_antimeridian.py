"""Route viewport filtering already survives the date line - this pins why.

``RouteQuerySet.intersecting_bbox`` builds the same
``Polygon.from_bbox((min_lng, min_lat, max_lng, max_lat))`` that broke
``PinQuerySet.within_bounds`` for viewports crossing the antimeridian, so it looks
like the same bug. It is not, and these tests exist to record the difference.

``Route.path`` is ``geography=True`` and this filter uses ``bboverlaps`` (PostGIS
``&&``), which is evaluated geodetically and handles the wrap itself: with the
box built naively, the query still returns the routes on screen. ``within_bounds``
differs by using ``__within`` (``ST_Within``), which has no geography
implementation and is evaluated as planar geometry - which is why *it* needed
splitting and this does not.

Confirmed rather than assumed: the same fixtures were run against the naive
single-box version and returned the correct routes.

So these are characterisation tests. If ``path`` ever becomes a plain geometry
column, or this filter moves to ``__within``, they fail - which is exactly when
the splitting logic would need to be added here too.
"""

from __future__ import annotations

from django.contrib.gis.geos import LineString
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.routes.model import Route


class RouteBboxAntimeridianTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = Profile.objects.get(user=baker.make("auth.User"))

    def _route(self, name: str, *points: tuple[float, float]) -> Route:
        return baker.make(Route, profile=self.profile, name=name, path=LineString(points, srid=4326))

    def _names(self, min_lat: float, min_lng: float, max_lat: float, max_lng: float) -> set[str]:
        return set(
            Route.objects.filter(profile=self.profile)
            .intersecting_bbox(min_lat, min_lng, max_lat, max_lng)
            .values_list("name", flat=True),
        )

    def test_an_ordinary_viewport_is_unaffected(self) -> None:
        self._route("inside", (-71.6, 41.2), (-71.4, 41.6))
        self._route("elsewhere", (-60.0, 41.2), (-59.0, 41.6))

        self.assertEqual(self._names(41.0, -72.0, 42.0, -71.0), {"inside"})

    def test_a_viewport_crossing_the_date_line_finds_routes_on_screen(self) -> None:
        self._route("west of line", (179.4, -15.2), (179.8, -14.8))
        self._route("east of line", (-179.8, -15.2), (-179.4, -14.8))
        self._route("far away", (0.0, -15.2), (0.4, -14.8))

        self.assertEqual(self._names(-20.0, 179.0, -10.0, -179.0), {"west of line", "east of line"})

    def test_a_crossing_viewport_excludes_the_far_side_of_the_planet(self) -> None:
        self._route("far away", (0.0, -15.2), (0.4, -14.8))

        self.assertEqual(self._names(-20.0, 179.0, -10.0, -179.0), set())

    def test_unwrapped_bounds_from_the_map_client_still_match(self) -> None:
        """Leaflet reports east=181 rather than -179 when panned across."""
        self._route("east of line", (-179.8, -15.2), (-179.4, -14.8))

        self.assertEqual(self._names(-20.0, 179.0, -10.0, 181.0), {"east of line"})

    def test_latitude_still_bounds_a_crossing_viewport(self) -> None:
        self._route("in latitude", (179.4, -15.2), (179.8, -14.8))
        self._route("too far north", (179.4, 40.0), (179.8, 41.0))

        self.assertEqual(self._names(-20.0, 179.0, -10.0, -179.0), {"in latitude"})
