"""A game area drawn across the date line must still find its pins.

SpotGuessr and Trivia both let a player restrict a session to an area they draw
on the map, stored as GeoJSON and queried with planar ``__within`` (``ST_Within``
has no geography implementation). An area drawn across the antimeridian arrives
with unwrapped coordinates - Leaflet gives 179 to 181 - which matches nothing on
its far side, so a player near the line sees "no eligible locations" for an area
full of their own pins.

Fixed at the source: ``GameConfig.geo_bounds`` splits before returning, so every
consumer inherits it - eligibility counts, round selection, and the external API's
eligible-pins endpoints - rather than each query site needing to remember. That
placement is the point, and is why this file tests the property rather than any
one caller.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.spotguessr.session import GameConfig as SpotGuessrConfig
from urbanlens.dashboard.services.trivia.session import TriviaConfig

_ACROSS = {
    "type": "Polygon",
    "coordinates": [[[179.0, -20.0], [181.0, -20.0], [181.0, -10.0], [179.0, -10.0], [179.0, -20.0]]],
}
_ORDINARY = {
    "type": "Polygon",
    "coordinates": [[[-72.0, 41.0], [-71.0, 41.0], [-71.0, 42.0], [-72.0, 42.0], [-72.0, 41.0]]],
}


class GameBoundsAntimeridianTests(SimpleTestCase):
    def test_both_games_fold_an_area_drawn_across_the_line(self) -> None:
        for label, config in (("spotguessr", SpotGuessrConfig), ("trivia", TriviaConfig)):
            with self.subTest(game=label):
                bounds = config(geo_bounds_geojson=_ACROSS).geo_bounds

                self.assertGreaterEqual(bounds.extent[0], -180.0, f"{label} left coordinates outside the stored range")
                self.assertLessEqual(bounds.extent[2], 180.0)

    def test_an_ordinary_area_is_untouched(self) -> None:
        """The rest of the planet must behave exactly as before."""
        for label, config in (("spotguessr", SpotGuessrConfig), ("trivia", TriviaConfig)):
            with self.subTest(game=label):
                bounds = config(geo_bounds_geojson=_ORDINARY).geo_bounds

                self.assertEqual(tuple(round(v, 6) for v in bounds.extent), (-72.0, 41.0, -71.0, 42.0))

    def test_no_restriction_stays_none(self) -> None:
        for label, config in (("spotguessr", SpotGuessrConfig), ("trivia", TriviaConfig)):
            with self.subTest(game=label):
                self.assertIsNone(config().geo_bounds)

    def test_the_split_area_still_covers_both_sides_of_the_line(self) -> None:
        """Folding must not silently drop half the area the player drew."""
        from django.contrib.gis.geos import Point

        bounds = SpotGuessrConfig(geo_bounds_geojson=_ACROSS).geo_bounds

        self.assertTrue(Point(179.5, -15.0, srid=4326).within(bounds), "lost the western half")
        self.assertTrue(Point(-179.5, -15.0, srid=4326).within(bounds), "lost the eastern half")

    def test_it_does_not_swallow_the_far_side_of_the_planet(self) -> None:
        from django.contrib.gis.geos import Point

        bounds = SpotGuessrConfig(geo_bounds_geojson=_ACROSS).geo_bounds

        self.assertFalse(Point(0.0, -15.0, srid=4326).within(bounds))
