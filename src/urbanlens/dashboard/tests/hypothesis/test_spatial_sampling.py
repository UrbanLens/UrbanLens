"""When a photo map is capped, the photos kept must still show the whole place.

The pin and wiki photo-map layers plotted every geotagged photo, so a location
with thousands of them built thousands of dicts - each resolving the uploader's
visibility - inside one request. Capping is the fix, but capping by "first N"
would be worse than useless on a map: photos cluster, so the first N are usually
the same doorway photographed a hundred times, and the rest of the site vanishes.

So the cap picks for *coverage* first and density second. The property that
matters is the third test here: an outlying photo survives a cap that a
take-the-first-N rule would have dropped.

Determinism matters too. The same request must return the same photos, or a
marker moves every time the layer reloads.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.geo.sampling import spread_across_space


def _clustered(count: int, start_id: int = 0) -> list[tuple[int, float, float]]:
    """Points crowded into a few metres, as repeat photos of one doorway are."""
    return [(start_id + i, 40.0 + i * 1e-6, -75.0 + i * 1e-6) for i in range(count)]


class TheSamplerTests(TestCase):
    def test_everything_under_the_limit_is_kept(self) -> None:
        rows = _clustered(5)
        self.assertEqual(len(spread_across_space(rows, 10)), 5)

    def test_the_limit_is_respected(self) -> None:
        self.assertEqual(len(spread_across_space(_clustered(500), 40)), 40)

    def test_an_outlier_survives_a_cap_that_first_n_would_drop(self) -> None:
        """The whole reason this is not a slice."""
        crowd = _clustered(200)
        outliers = [(900, 41.5, -74.0), (901, 39.2, -76.8), (902, 40.9, -73.1)]
        kept = set(spread_across_space([*crowd, *outliers], 10))

        for outlier_id, _lat, _lon in outliers:
            self.assertIn(outlier_id, kept, "a distant photo was dropped in favour of more of the same doorway")

    def test_it_is_deterministic(self) -> None:
        rows = [*_clustered(300), (900, 41.5, -74.0)]
        self.assertEqual(spread_across_space(rows, 25), spread_across_space(rows, 25))

    def test_every_id_returned_came_from_the_input(self) -> None:
        rows = _clustered(80)
        ids = {row[0] for row in rows}
        self.assertTrue(set(spread_across_space(rows, 20)).issubset(ids))

    def test_no_id_is_returned_twice(self) -> None:
        kept = spread_across_space([*_clustered(300), (900, 41.5, -74.0)], 50)
        self.assertEqual(len(kept), len(set(kept)))

    def test_identical_coordinates_do_not_break_it(self) -> None:
        """Several photos from one spot is the ordinary case, not an edge case."""
        rows = [(i, 40.0, -75.0) for i in range(50)]
        self.assertEqual(len(spread_across_space(rows, 10)), 10)

    def test_an_empty_input_gives_an_empty_result(self) -> None:
        self.assertEqual(spread_across_space([], 10), [])

    def test_a_limit_of_zero_keeps_nothing(self) -> None:
        self.assertEqual(spread_across_space(_clustered(5), 0), [])
