"""`RedataBasemapTilesGateway.list_sources` envelope parsing, at the unit level.

`test_basemap_tile_proxy.py` mocks `list_sources`/`download_tile` at the
controller boundary, so the gateway's own body-shape handling had no direct test
anywhere: the bare-list case, the `{"sources": [...]}` and `{"results": [...]}`
envelopes, their fallback order, and the `row.get("id")` filter. Every existing
fixture uses the `sources` key with well-formed rows, so swapping the fallback
order or dropping the filter would have broken nothing visible.

Mocked at `get_json` rather than at `session`: `get_json` is the seam between
"talk to REData" and "make sense of the answer", and it is only the second half
that is untested. The transport half is exercised through the base gateway.

Why this matters more than a normal parsing helper: reading the wrong key fails
*silently* as "this deployment offers no layers", which is indistinguishable
from a deployment that genuinely offers none. The gateway's own comment says so.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway

_ROW = {"id": "osm", "url_template": "https://x/{z}/{x}/{y}.png", "name": "OSM"}
_OTHER = {"id": "sat", "url_template": "https://y/{z}/{x}/{y}.png", "name": "Satellite"}


def _sources(body: Any) -> list[dict[str, Any]]:
    """Run `list_sources` against one REData body shape."""
    with mock.patch.object(RedataBasemapTilesGateway, "get_json", return_value=body):
        return RedataBasemapTilesGateway().list_sources()


class ListSourcesEnvelopeTests(SimpleTestCase):
    """The three body shapes the gateway accepts, and the order it prefers them."""

    def test_a_bare_list_is_returned_as_is(self) -> None:
        self.assertEqual(_sources([_ROW]), [_ROW])

    def test_the_sources_envelope_is_unwrapped(self) -> None:
        self.assertEqual(_sources({"sources": [_ROW]}), [_ROW])

    def test_the_results_envelope_is_unwrapped(self) -> None:
        self.assertEqual(_sources({"results": [_ROW]}), [_ROW])

    def test_sources_wins_over_results_when_both_are_present(self) -> None:
        """The documented order: this endpoint answers `sources`, not the paginated `results`."""
        self.assertEqual(_sources({"sources": [_ROW], "results": [_OTHER]}), [_ROW])

    def test_results_is_used_when_sources_is_empty(self) -> None:
        """`or` rather than `in`: an empty `sources` falls through rather than winning."""
        self.assertEqual(_sources({"sources": [], "results": [_OTHER]}), [_OTHER])


class ListSourcesRowFilterTests(SimpleTestCase):
    """A row without an `id` cannot be requested, so it must not be offered."""

    def test_a_row_with_no_id_is_dropped(self) -> None:
        self.assertEqual(_sources([_ROW, {"url_template": "https://z/{z}/{x}/{y}.png"}]), [_ROW])

    def test_a_row_with_an_empty_id_is_dropped(self) -> None:
        self.assertEqual(_sources([_ROW, {"id": "", "name": "Nameless"}]), [_ROW])

    def test_a_non_dict_row_is_dropped(self) -> None:
        self.assertEqual(_sources([_ROW, "osm", None, 7, ["osm"]]), [_ROW])

    def test_every_well_formed_row_survives(self) -> None:
        """Anti-vacuity: the filter must not be dropping everything."""
        self.assertEqual(_sources([_ROW, _OTHER]), [_ROW, _OTHER])


class ListSourcesEmptyAnswerTests(SimpleTestCase):
    """Nothing usable answers as no layers, rather than raising."""

    def test_an_unconfigured_or_empty_answer_is_no_layers(self) -> None:
        for body in (None, {}, [], {"sources": []}, {"results": []}, {"sources": None}):
            with self.subTest(body=body):
                self.assertEqual(_sources(body), [])

    def test_an_unexpected_scalar_body_is_no_layers(self) -> None:
        for body in ("nonsense", 7, True):
            with self.subTest(body=body):
                self.assertEqual(_sources(body), [])

    def test_an_envelope_holding_a_non_list_is_no_layers(self) -> None:
        """`body.get("sources")` is trusted to be iterable; a string would iterate per character."""
        self.assertEqual(_sources({"sources": {"id": "osm"}}), [])


class EndpointForLogTests(SimpleTestCase):
    """The tile path must be truncated at the layer, never logged whole.

    A tile URL is `/tiles/{layer}/{z}/{x}/{y}/`, so logging it verbatim would
    accumulate a record of which places this deployment's users panned over.
    """

    def test_a_tile_url_is_truncated_at_the_layer(self) -> None:
        self.assertEqual(
            RedataBasemapTilesGateway.endpoint_for_log("https://redata.test/api/v1/tiles/osm/12/345/678/"),
            "https://redata.test/api/v1/tiles/osm/",
        )

    def test_no_coordinate_survives_truncation(self) -> None:
        logged = RedataBasemapTilesGateway.endpoint_for_log("https://redata.test/api/v1/tiles/osm/12/345/678/")
        for coordinate in ("12", "345", "678"):
            self.assertNotIn(coordinate, logged.removeprefix("https://redata.test/api/v1/tiles/"))

    def test_the_catalogue_url_keeps_its_shape(self) -> None:
        self.assertEqual(
            RedataBasemapTilesGateway.endpoint_for_log("https://redata.test/api/v1/tiles/sources/"),
            "https://redata.test/api/v1/tiles/sources/",
        )

    def test_a_url_outside_the_tile_namespace_is_left_alone(self) -> None:
        self.assertEqual(
            RedataBasemapTilesGateway.endpoint_for_log("https://redata.test/api/v1/weather/"),
            "https://redata.test/api/v1/weather/",
        )
