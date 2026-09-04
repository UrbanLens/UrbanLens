"""REData's basemap layers, proxied so its API key stays server-side.

REData publishes a tile catalogue and proxies the vendors behind it, which
buys layers this application would otherwise register for itself, one
attribution source of truth, and a cache that spares the vendor a request per
pan. Nothing consumed either endpoint until now.

The behaviour worth pinning is the caching contract, because getting it wrong
is invisible: REData distinguishes "the vendor confirms no such tile" (404,
cacheable) from "the vendor could not be reached" (503, never cacheable), and
caching the latter turns a passing outage into a permanently blank region of
the map - the same defect class the outage-cache check exists for.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase

_GATEWAY = "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway"
#: Patched at its source module, not in the controller's namespace: the
#: controller imports it inside the request method, so there is no attribute
#: on the controller module to replace.
_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"


class BasemapTileProxyTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})

    def test_a_tile_is_served_and_cached(self) -> None:
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNGDATA", "image/png")) as download,
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, b"PNGDATA")
        self.assertEqual(second.content, b"PNGDATA")
        self.assertEqual(download.call_count, 1, "a served tile must not be re-fetched on the next pan")

    def test_the_vendor_content_type_is_preserved_through_the_cache(self) -> None:
        """Not every layer is PNG; the cache stores ``(body, content_type)`` as
        a pair. A cache-hit path that dropped the type (or hardcoded
        image/png) would only surface once a non-PNG layer was already
        cached - exactly the failure the controller's own comment warns about."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"WEBPDATA", "image/webp")),
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first["Content-Type"], "image/webp")
        self.assertEqual(
            second["Content-Type"], "image/webp", "the cache-hit path must serve the same type as the fresh fetch"
        )

    def test_a_definitive_miss_is_cached(self) -> None:
        """Most of a layer's pyramid is empty; re-asking on every pan is the cost."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(404, b"", "")) as download,
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(download.call_count, 1)

    def test_a_400_is_also_a_definitive_miss(self) -> None:
        """400 (invalid_parameter/unknown_layer) is as definitive as 404 - a
        cache branch narrowed to ``status == 404`` would still pass every
        other test in this file."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(400, b"", "")) as download,
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(download.call_count, 1, "a 400 must be cached too, not just a 404")

    def test_a_vendor_outage_is_never_cached(self) -> None:
        """The whole point: an outage must not become a permanently blank map."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(503, b"", "")) as download,
        ):
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(download.call_count, 2, "caching a 503 memorises an outage as 'no map here'")

    def test_a_failed_request_answers_503_rather_than_500(self) -> None:
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(
                f"{_GATEWAY}.download_tile", side_effect=LocationContextUnavailableError("source_error", "down")
            ),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)

    def test_a_network_failure_answers_503_rather_than_500(self) -> None:
        """OSError - a real ``requests`` connection failure, distinct from the
        gateway's own structured error type - is the other member of the
        except tuple; dropping it from the tuple would 500 on an outage."""
        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.download_tile", side_effect=OSError("connection reset")),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)

    def test_unconfigured_redata_is_a_404_not_a_crash(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_proxy_requires_a_login(self) -> None:
        self.client.logout()

        self.assertNotEqual(self.client.get(self.url).status_code, 200)


class BasemapCatalogueTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles.sources")

    def _sources(self, rows):
        return mock.patch(f"{_GATEWAY}.list_sources", return_value=rows)

    def test_layers_point_at_the_proxy_not_the_vendor(self) -> None:
        """Handing the browser REData's own template gives it a layer it cannot load."""
        rows = [
            {
                "id": "usgs-topo",
                "name": "USGS Topo",
                "attribution": "USGS",
                "url_template": "https://redata.example/api/v1/tiles/usgs-topo/{z}/{x}/{y}/",
            }
        ]

        with mock.patch(_CONFIGURED, return_value=True), self._sources(rows):
            layers = self.client.get(self.url).json()["layers"]

        self.assertEqual(len(layers), 1)
        self.assertNotIn("redata.example", layers[0]["url_template"])
        # Filling the template in the way Leaflet would - and comparing against
        # a URL this view is independently known to serve - pins the sentinel
        # substitution itself, not just the fixed prefix around it. A broken
        # replace() (wrong sentinel, or none at all) would still satisfy a
        # bare substring check on the prefix while handing the browser
        # "900001" instead of "{z}".
        filled = layers[0]["url_template"].replace("{z}", "12").replace("{x}", "1204").replace("{y}", "1539")
        self.assertEqual(
            filled, reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})
        )

    def test_a_layer_without_attribution_is_not_offered(self) -> None:
        """Every vendor here requires attribution on the rendered map."""
        with mock.patch(_CONFIGURED, return_value=True), self._sources([{"id": "mystery", "name": "Mystery"}]):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

    def test_an_unreachable_catalogue_is_not_cached_as_empty(self) -> None:
        """Otherwise one bad moment costs this deployment its extra layers for a day."""
        from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError

        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch(f"{_GATEWAY}.list_sources", side_effect=LocationContextUnavailableError("source_error", "down")),
        ):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]
        with mock.patch(_CONFIGURED, return_value=True), self._sources(rows):
            self.assertEqual(len(self.client.get(self.url).json()["layers"]), 1)

    def test_the_catalogue_is_cached_between_requests(self) -> None:
        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]

        with mock.patch(_CONFIGURED, return_value=True), self._sources(rows) as list_sources:
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(list_sources.call_count, 1, "documented as called once per session, not once per map load")

    def test_unconfigured_redata_yields_no_layers(self) -> None:
        with mock.patch(_CONFIGURED, return_value=False):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])


class TileLogPrivacyTests(SimpleTestCase):
    """A tile URL is a coordinate somebody was looking at.

    `ApiCallLog` records the endpoint of every gateway call to track volume and
    cost per service. For every other service the URL is a point lookup the
    application already knows about; for tiles it is one request per pan, so
    logging the path verbatim would accumulate a record of which places this
    deployment's users panned over - in an application whose premise is that
    pin locations are private.

    The layer answers everything the log is for. The coordinate does not.
    """

    def _normalize(self, url: str) -> str:
        from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway

        return RedataBasemapTilesGateway.endpoint_for_log(url)

    def test_the_tile_coordinate_is_not_logged(self) -> None:
        logged = self._normalize("https://redata.example/api/v1/tiles/usgs-topo/18/77238/98543/")

        self.assertNotIn("77238", logged)
        self.assertNotIn("98543", logged)
        self.assertNotIn("/18/", logged)

    def test_the_layer_is_kept(self) -> None:
        """Volume and cost per layer is the question the log exists to answer."""
        logged = self._normalize("https://redata.example/api/v1/tiles/usgs-topo/18/77238/98543/")

        self.assertEqual(logged, "https://redata.example/api/v1/tiles/usgs-topo/")

    def test_the_catalogue_url_is_unchanged(self) -> None:
        url = "https://redata.example/api/v1/tiles/sources/"

        self.assertEqual(self._normalize(url), url)

    def test_an_unrelated_url_passes_through(self) -> None:
        url = "https://redata.example/api/v1/imagery/?lat=41.7&lng=-73.9"

        self.assertEqual(self._normalize(url), url)

    def test_the_default_gateway_still_logs_the_full_url(self) -> None:
        """Only this gateway opts out; the rest keep the detail they rely on."""
        from urbanlens.dashboard.services.core.gateway import Gateway

        url = "https://redata.example/api/v1/geocode/?q=poughkeepsie"
        self.assertEqual(Gateway.endpoint_for_log(url), url)
