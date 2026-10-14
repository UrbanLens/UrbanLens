"""The tile catalogue's basemap layers, proxied so the service's credentials stay server-side."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers import basemap_tiles
from urbanlens.dashboard.services.map.basemap_vendors import VENDOR_TILES
from urbanlens.dashboard.tests.hypothesis.tile_catalogue_fake import FakeCatalogueMixin, catalogue_off, catalogue_on
from urbanlens.UrbanLens.settings.app import settings as app_settings

_GATEWAY = "urbanlens.dashboard.tests.hypothesis.tile_catalogue_fake.FakeTileCatalogue"


class BasemapTileProxyTests(FakeCatalogueMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})

    def test_a_tile_is_served_and_cached(self) -> None:
        with (
            catalogue_on(),
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
        """Not every layer is PNG; the cache stores ``(body, content_type)`` as a pair. A cache-hit path that dropped the type (or hardcoded image/png) would only surface once a non-PNG layer was already cached - exactly the failure the controller's own comment warns about."""
        with (
            catalogue_on(),
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
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(404, b"", "")) as download,
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(download.call_count, 1)

    def test_a_400_is_also_a_definitive_miss(self) -> None:
        """
        400 (invalid_parameter/unknown_layer) is as definitive as 404 - a
        cache branch narrowed to ``status == 404`` would still pass every
        other test in this file."""
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(400, b"", "")) as download,
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(download.call_count, 1, "a 400 must be cached too, not just a 404")

    def test_a_layer_switched_to_vector_is_not_remembered_as_a_missing_tile(self) -> None:
        """
        ``vector_layer_not_served`` answers a question about the layer, not the coordinate.
        Caching a layer-level refusal per coordinate would cause an outage when the layer
        switches back to raster, until those entries expire.
        """
        refusal = b'{"error":"vector_layer_not_served","message":"\'street\' is a vector layer published at \'https://tiles.example.com/styles/street.json\'"}'
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(400, refusal, "application/json")) as download,
        ):
            first = self.client.get(self.url)
            second = self.client.get(self.url)

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        self.assertEqual(download.call_count, 2, "a layer-level refusal must not be cached as a per-coordinate miss")

    def test_a_layer_switched_to_vector_drops_the_cached_catalogue(self) -> None:
        """
        The catalogue is what told the client this layer was raster, so it is the stale thing.

        Same reasoning as the ``ServiceDisabledError`` branch: a map draws what the catalogue named,
        so leaving a catalogue that advertises a proxied ``url_template`` for a layer the service now
        refuses tile-by-tile keeps every client asking for tiles nothing can serve.
        """
        from urbanlens.dashboard.services.map.basemap_catalogue import CATALOGUE_CACHE_KEY

        cache.set(
            CATALOGUE_CACHE_KEY, [{"id": "street", "source_type": "raster", "url_template": "/x/{z}/{x}/{y}/"}], 86400
        )
        refusal = b'{"error":"vector_layer_not_served","message":"a vector layer"}'
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(400, refusal, "application/json")),
        ):
            self.client.get(self.url)

        self.assertIsNone(
            cache.get(CATALOGUE_CACHE_KEY), "the catalogue advertising a now-vector layer as raster must be dropped"
        )

    def test_an_ordinary_400_is_still_cached(self) -> None:
        """
        The narrowing above must not cost the common case: an out-of-range coordinate is still
        a definitive per-coordinate answer and is still worth remembering."""
        with (
            catalogue_on(),
            mock.patch(
                f"{_GATEWAY}.download_tile", return_value=(400, b'{"error":"invalid_parameter"}', "application/json")
            ) as download,
        ):
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(download.call_count, 1)

    def test_a_vendor_outage_is_never_cached(self) -> None:
        """The whole point: an outage must not become a permanently blank map."""
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(503, b"", "")) as download,
        ):
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(download.call_count, 2, "caching a 503 memorises an outage as 'no map here'")

    def test_a_failed_request_answers_503_rather_than_500(self) -> None:
        from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError

        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", side_effect=ProviderUnavailableError("down")),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)

    def test_a_network_failure_answers_503_rather_than_500(self) -> None:
        """
        OSError - a real ``requests`` connection failure, distinct from the
        gateway's own structured error type - is the other member of the
        except tuple; dropping it from the tuple would 500 on an outage."""
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", side_effect=OSError("connection reset")),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)

    def test_an_oversized_upstream_body_answers_503_rather_than_500(self) -> None:
        """
        The third thing ``download_tile`` can raise, and the one the except tuple missed.
        ``read_capped`` refuses a body over 25MB - or one the gateway forgot to stream - with a
        bare ``GatewayRequestError``, which is neither an ``OSError`` nor the gateway's structured
        type. A viewport asks for ~30 tiles at once and nothing suppresses the retry, so one
        misbehaving layer 500s on repeat instead of degrading, and the client's backoff never
        engages because a 500 carries no Retry-After."""
        from urbanlens.dashboard.services.core.gateway import GatewayRequestError

        with (
            catalogue_on(),
            mock.patch(
                f"{_GATEWAY}.download_tile",
                side_effect=GatewayRequestError("basemap tile is larger than the 25MB limit for proxied media"),
            ),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)

    def test_no_catalogue_is_a_404_not_a_crash(self) -> None:
        with catalogue_off():
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_proxy_requires_a_login(self) -> None:
        self.client.logout()

        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_a_refused_call_stops_the_catalogue_offering_layers_this_deployment_cannot_serve(self) -> None:
        """
        The catalogue is cached for a day, and a map that draws only what the catalogue named is
        entirely grey for that day once this deployment can no longer call the catalogue's service at all - a
        disabled service, an exhausted budget, an environment that permits no outbound calls. The
        vendor layers it replaced were working; dropping the catalogue puts them back."""
        from urbanlens.dashboard.services.core.rate_limiter import ServiceDisabledError
        from urbanlens.dashboard.services.map import basemap_catalogue

        cache.set(basemap_catalogue.CATALOGUE_CACHE_KEY, [{"id": "street", "attribution": "Example Tiles"}], 60)
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", side_effect=ServiceDisabledError("tile_catalogue")),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 503)
        self.assertIsNone(cache.get(basemap_catalogue.CATALOGUE_CACHE_KEY))
        self.assertEqual(basemap_catalogue.basemap_tile_catalogue(allow_fetch=False), [])

    def test_a_busy_minute_keeps_the_catalogue(self) -> None:
        """
        Being over budget, or unable to reach the vendor, is not evidence this deployment cannot
        serve the layer at all - and rebuilding the catalogue costs a ~1.45s call to the service,
        so dropping it every time a burst crosses the limit would flap under exactly the load that
        caused it."""
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError
        from urbanlens.dashboard.services.map import basemap_catalogue

        cached = [{"id": "street", "attribution": "Example Tiles"}]
        for failure in (RateLimitExceededError("tile_catalogue"), OSError("connection reset")):
            with self.subTest(failure=type(failure).__name__):
                cache.set(basemap_catalogue.CATALOGUE_CACHE_KEY, cached, 60)
                with (
                    catalogue_on(),
                    mock.patch(f"{_GATEWAY}.download_tile", side_effect=failure),
                ):
                    self.client.get(self.url)

                self.assertEqual(cache.get(basemap_catalogue.CATALOGUE_CACHE_KEY), cached)


class BasemapCatalogueTests(FakeCatalogueMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.url = reverse("map.basemap_tiles.sources")
        # The self-hosted path; a configured Protomaps key re-points street and dark (test_basemap_protomaps_style).
        patcher = mock.patch.object(app_settings, "protomaps_api_key", "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sources(self, rows):
        return mock.patch(f"{_GATEWAY}.list_sources", return_value=rows)

    def test_layers_point_at_the_proxy_not_the_vendor(self) -> None:
        """Handing the browser the catalogue service's own template gives it a layer it cannot load."""
        rows = [
            {
                "id": "usgs-topo",
                "name": "USGS Topo",
                "attribution": "USGS",
                "url_template": "https://catalogue.example/api/v1/tiles/usgs-topo/{z}/{x}/{y}/",
            }
        ]

        with catalogue_on(), self._sources(rows):
            layers = self.client.get(self.url).json()["layers"]

        self.assertEqual(len(layers), 1)
        self.assertNotIn("catalogue.example", layers[0]["url_template"])
        # Filling the template in the way Leaflet would - and comparing against a URL this view is independently
        # known to serve - pins the sentinel substitution itself, not just the fixed prefix around it.
        filled = layers[0]["url_template"].replace("{z}", "12").replace("{x}", "1204").replace("{y}", "1539")
        self.assertEqual(
            filled, reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": 1204, "y": 1539})
        )

    def test_a_layer_without_attribution_is_not_offered(self) -> None:
        """Every vendor here requires attribution on the rendered map."""
        with catalogue_on(), self._sources([{"id": "mystery", "name": "Mystery"}]):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

    def test_an_unreachable_catalogue_is_not_cached_as_empty(self) -> None:
        """Otherwise one bad moment costs this deployment its extra layers for a day."""
        from urbanlens.dashboard.services.core.gateway import ProviderUnavailableError

        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.list_sources", side_effect=ProviderUnavailableError("down")),
        ):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]
        with catalogue_on(), self._sources(rows):
            self.assertEqual(len(self.client.get(self.url).json()["layers"]), 1)

    def test_the_catalogue_is_cached_between_requests(self) -> None:
        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]

        with catalogue_on(), self._sources(rows) as list_sources:
            self.client.get(self.url)
            self.client.get(self.url)

        self.assertEqual(list_sources.call_count, 1, "documented as called once per session, not once per map load")

    def test_no_catalogue_yields_no_layers(self) -> None:
        with catalogue_off():
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

    def test_a_raster_entry_without_source_type_is_treated_as_raster(self) -> None:
        """A catalogue from before source types existed sends no source_type field at all."""
        rows = [{"id": "usgs-topo", "name": "USGS Topo", "attribution": "USGS"}]

        with catalogue_on(), self._sources(rows):
            layers = self.client.get(self.url).json()["layers"]

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["source_type"], "raster")
        self.assertIn("url_template", layers[0])
        self.assertNotIn("style_url", layers[0])

    def test_a_vector_entry_passes_through_its_style_url_unproxied(self) -> None:
        """The service never proxies a single vector tile - the client fetches style_url directly, so this view must not try to rewrite it into a proxy url_template the way a raster entry gets."""
        rows = [
            {
                "id": "street",
                "name": "Street",
                "source_type": "vector",
                "attribution": "OSM",
                "style_url": "https://catalogue.example/styles/street.json",
            }
        ]

        with catalogue_on(), self._sources(rows):
            layers = self.client.get(self.url).json()["layers"]

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["source_type"], "vector")
        self.assertEqual(layers[0]["style_url"], "https://catalogue.example/styles/street.json")
        self.assertNotIn("url_template", layers[0])

    def test_a_vector_entry_with_no_style_url_is_not_offered(self) -> None:
        rows = [{"id": "street", "name": "Street", "source_type": "vector", "attribution": "OSM"}]

        with catalogue_on(), self._sources(rows):
            self.assertEqual(self.client.get(self.url).json()["layers"], [])

    def test_a_entry_keeps_both_shapes(self) -> None:
        """
        A self-hosted layer serves both ways, so the rewriter must keep both
        entries so clients can choose vector or raster."""
        rows = [_STREET_ROW]

        with catalogue_on(), self._sources(rows):
            layers = self.client.get(self.url).json()["layers"]

        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0]["style_url"], "https://tiles.example/street.json")
        self.assertEqual(
            layers[0]["url_template"],
            reverse("map.basemap_tiles", kwargs={"layer": "street", "z": 900001, "x": 900002, "y": 900003})
            .replace("900001", "{z}")
            .replace("900002", "{x}")
            .replace("900003", "{y}")
            + f"?v={VENDOR_TILES['street'].cache_tag}",
        )

    def test_a_entry_carries_the_raster_half_s_own_credit_and_depth(self) -> None:
        """
        The two halves are different datasets, so a client drawing one must not show the other's
        attribution or pan past its ceiling."""
        with catalogue_on(), self._sources([_STREET_ROW]):
            entry = self.client.get(self.url).json()["layers"][0]

        self.assertEqual(entry["attribution"], "OSM/Protomaps")
        self.assertEqual(entry["max_zoom"], 15)
        self.assertEqual(entry["fallback_attribution"], "Esri")
        self.assertEqual(entry["fallback_max_zoom"], 19)
        self.assertEqual(entry["fallback_min_zoom"], 0)


#: A `street` entry with a self-hosted vector style *and* a proxied raster half.
_STREET_ROW = {
    "id": "street",
    "name": "Street",
    "source_type": "vector",
    "attribution": "OSM/Protomaps",
    "min_zoom": 0,
    "max_zoom": 15,
    "style_url": "https://tiles.example/street.json",
    "url_template": "https://catalogue.example/api/v1/tiles/street/{z}/{x}/{y}/",
    "fallback_attribution": "Esri",
    "fallback_min_zoom": 0,
    "fallback_max_zoom": 19,
}


class SignedOutCatalogueTests(FakeCatalogueMixin, TestCase):
    """
    What a public share page may be handed: the proxy is login-required, so a template offered to
    an anonymous visitor paints 404s where a working layer would be."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)
        # The self-hosted path; a configured Protomaps key re-points street and dark (test_basemap_protomaps_style).
        patcher = mock.patch.object(app_settings, "protomaps_api_key", "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _viewer(self, rows, *, authenticated: bool):
        from urbanlens.dashboard.services.map.basemap_catalogue import catalogue_for_viewer

        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.list_sources", return_value=rows),
        ):
            return catalogue_for_viewer(authenticated=authenticated)

    def test_a_signed_out_viewer_is_offered_the_style_without_the_proxy_template(self) -> None:
        """
        Dropping the raster *entries* is no longer enough once a vector entry carries a template
        of its own - it has to come off the entries that stay."""
        entries = self._viewer([_STREET_ROW], authenticated=False)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["style_url"], "https://tiles.example/street.json")
        self.assertNotIn("url_template", entries[0])

    def test_a_signed_out_viewer_is_offered_no_raster_only_layer(self) -> None:
        rows = [
            {
                "id": "satellite",
                "name": "Satellite",
                "attribution": "Esri",
                "url_template": "https://catalogue.example/t/{z}/{x}/{y}/",
            }
        ]

        self.assertEqual(self._viewer(rows, authenticated=False), [])

    def test_a_signed_in_viewer_keeps_the_proxy_template(self) -> None:
        entries = self._viewer([_STREET_ROW], authenticated=True)

        self.assertIn("url_template", entries[0])

    def test_stripping_the_template_does_not_mutate_the_shared_catalogue(self) -> None:
        """The entries are the cached catalogue every other reader gets handed."""
        self._viewer([_STREET_ROW], authenticated=False)
        # Cached now, so this reads the same dicts the signed-out pass just filtered.
        self.assertIn("url_template", self._viewer([_STREET_ROW], authenticated=True)[0])


class BasemapTileConcurrencyTests(FakeCatalogueMixin, TestCase):
    """
    The bound that keeps a cold map load from occupying every request thread in the process.

    A viewport is ~30 tiles requested at once and each uncached one blocks on a slow upstream, so
    without this the proxy is a site-wide stall waiting for someone to open a map.

    The slot is held directly rather than from a second thread: Django's ``TestCase`` wraps each
    test in a transaction its own connection owns, so a request served on another thread cannot
    see the user this one just created and never reaches the code under test.
    """

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        basemap_tiles.UpstreamSlots.reset()
        self.addCleanup(basemap_tiles.UpstreamSlots.reset)

    def _url(self, x: int) -> str:
        # A layer `VENDOR_TILES` does not name, like the rest of this file: the slot cap is the
        # view's and applies to either upstream, but `street` would route past `_GATEWAY`'s patch
        # to the real vendor rather than fail.
        return reverse("map.basemap_tiles", kwargs={"layer": "usgs-topo", "z": 12, "x": x, "y": 1539})

    @contextlib.contextmanager
    def _every_slot_taken(self) -> Iterator[None]:
        """Run the block with the process's only upstream slot already held by someone else."""
        with mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", 1):
            basemap_tiles.UpstreamSlots.reset()
            self.assertTrue(basemap_tiles.UpstreamSlots.semaphore().acquire(blocking=False))
            try:
                yield
            finally:
                basemap_tiles.UpstreamSlots.semaphore().release()

    def test_a_tile_over_the_cap_is_refused_without_reaching_the_upstream(self) -> None:
        """503 at once, not a wait: a thread blocked waiting for a slot is occupying the very resource the slot rations, so queueing would defend nothing."""
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNGDATA", "image/png")) as download,
            self._every_slot_taken(),
        ):
            refused = self.client.get(self._url(1205))

        self.assertEqual(refused.status_code, 503)
        self.assertEqual(download.call_count, 0, "the request must be refused before it costs an upstream call")
        self.assertIn(
            "Retry-After",
            refused.headers,
            "a refusal is 'ask again shortly', and the client retries it rather than leaving a hole in the map",
        )

    def test_a_refusal_is_not_cached(self) -> None:
        """Caching it would turn a momentary burst into a permanently blank square for a week."""
        url = self._url(1206)
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNGDATA", "image/png")),
        ):
            with self._every_slot_taken():
                self.assertEqual(self.client.get(url).status_code, 503)
            served = self.client.get(url)

        self.assertEqual(served.status_code, 200, "the refusal must have left nothing behind")
        self.assertEqual(served.content, b"PNGDATA")

    def test_a_cached_tile_never_touches_the_cap(self) -> None:
        """The bound is on the slow path only; a warm tile must serve no matter how busy the upstream is."""
        url = self._url(1207)
        with (
            catalogue_on(),
            mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNGDATA", "image/png")),
        ):
            self.assertEqual(self.client.get(url).status_code, 200)
            with self._every_slot_taken():
                served = self.client.get(url)

        self.assertEqual(served.status_code, 200, "a cached tile must not be refused")
        self.assertEqual(served.content, b"PNGDATA")

    def test_the_slot_is_released_when_the_upstream_raises(self) -> None:
        """A leaked slot would permanently shrink this process's capacity after one network blip."""
        with (
            catalogue_on(),
            mock.patch.object(basemap_tiles.app_settings, "basemap_tile_upstream_concurrency", 1),
        ):
            basemap_tiles.UpstreamSlots.reset()
            with mock.patch(f"{_GATEWAY}.download_tile", side_effect=OSError("boom")):
                self.assertEqual(self.client.get(self._url(1208)).status_code, 503)
            with mock.patch(f"{_GATEWAY}.download_tile", return_value=(200, b"PNGDATA", "image/png")):
                recovered = self.client.get(self._url(1209))

        self.assertEqual(recovered.status_code, 200, "the failed fetch must have handed its slot back")
