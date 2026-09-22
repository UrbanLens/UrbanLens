"""Which upstream a raster basemap tile is fetched from, and what URL it asks for.

Going straight to the vendor removes a round trip that measured as most of the cost of a tile, but
it moves the URL-building into this codebase - and a wrong tile URL is the kind of bug that returns
200 with a real image of the wrong place. Nothing downstream can catch that: the bytes are valid,
the type is right, and the cache stores it for a week.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from urbanlens.dashboard.controllers.basemap_tiles import _fetch_tile
from urbanlens.dashboard.services.apis.locations.basemap_vendor_tiles_gateway import BasemapVendorTilesGateway
from urbanlens.dashboard.services.map.basemap_catalogue import _offered_layers
from urbanlens.dashboard.services.map.basemap_vendors import VENDOR_TILES, VendorTiles, vendor_for


class VendorUrlTemplateTests(SimpleTestCase):
    """The axis order, which differs by vendor and is invisible when wrong."""

    def test_every_esri_template_puts_the_row_before_the_column(self) -> None:
        """Esri's REST tile route is `/tile/{z}/{y}/{x}`. Swapped, every tile is a real image of
        somewhere else - mirrored about the diagonal, and only obviously wrong near the poles."""
        for layer, vendor in VENDOR_TILES.items():
            if "arcgisonline.com" not in vendor.url_template:
                continue
            self.assertTrue(vendor.url_template.endswith("/tile/{z}/{y}/{x}"), f"{layer}: {vendor.url_template}")
            self.assertTrue(vendor.url_for(z=14, x=4841, y=6170).endswith("/tile/14/6170/4841"), layer)

    def test_an_xyz_template_keeps_the_column_before_the_row(self) -> None:
        """No layer in the table is XYZ today, but `url_for` is what decides the order, and the
        whole class of bug is invisible downstream."""
        vendor = VendorTiles(url_template="https://tile.example.test/{z}/{x}/{y}.png")

        self.assertTrue(vendor.url_for(z=14, x=4841, y=6170).endswith("/14/4841/6170.png"))

    def test_every_template_consumes_all_three_coordinates(self) -> None:
        """A template missing a placeholder silently serves one tile for a whole column or row."""
        for layer, vendor in VENDOR_TILES.items():
            url = vendor.url_for(z=7, x=11, y=22)
            self.assertNotIn("{", url, f"{layer} left a placeholder unfilled: {url}")
            for axis, value in (("z", "7"), ("x", "11"), ("y", "22")):
                self.assertIn(
                    value, url.rsplit("/MapServer/", 1)[-1] if "/MapServer/" in url else url, f"{layer} dropped {axis}"
                )

    def test_every_template_is_https(self) -> None:
        for layer, vendor in VENDOR_TILES.items():
            self.assertTrue(vendor.url_template.startswith("https://"), layer)

    def test_a_subdomain_is_chosen_from_the_declared_set_and_is_stable(self) -> None:
        """Round-robin would send the same tile to a different host on every retry, so a host that
        already has it warm is the one least likely to be asked."""
        vendor = VendorTiles(url_template="https://{s}.example.test/{z}/{x}/{y}.png", subdomains=("a", "b", "c"))

        hosts = {vendor.url_for(z=5, x=i, y=3).split("//")[1].split(".")[0] for i in range(12)}
        self.assertEqual(hosts, {"a", "b", "c"})
        self.assertEqual(vendor.url_for(z=5, x=7, y=3), vendor.url_for(z=5, x=7, y=3))

    def test_a_template_without_a_subdomain_placeholder_is_left_alone(self) -> None:
        self.assertNotIn("{s}", VENDOR_TILES["satellite"].url_for(z=1, x=1, y=1))

    def test_no_vendor_here_is_carto(self) -> None:
        """CARTO was never a deliberate choice for this project and `dark` was the last reference."""
        for layer, vendor in VENDOR_TILES.items():
            self.assertNotIn("cartocdn", vendor.url_template, layer)


class WhichUpstreamAnswersTests(SimpleTestCase):
    """The routing decision itself."""

    def test_a_layer_with_a_vendor_never_reaches_redata(self) -> None:
        with (
            mock.patch.object(
                BasemapVendorTilesGateway, "download_tile", return_value=(200, b"png", "image/png")
            ) as vendor,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway.download_tile"
            ) as redata,
        ):
            result = _fetch_tile("satellite", 12, 1205, 1539)

        self.assertEqual(result, (200, b"png", "image/png"))
        vendor.assert_called_once_with("satellite", 12, 1205, 1539)
        redata.assert_not_called()

    def test_a_layer_with_no_vendor_still_goes_through_redata(self) -> None:
        """REData may publish a layer this table does not name - a future one, or a deployment's
        own. Dropping it would be a blank layer rather than a slower one."""
        self.assertIsNone(vendor_for("some_future_layer"))

        with (
            mock.patch.object(BasemapVendorTilesGateway, "download_tile") as vendor,
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway.RedataBasemapTilesGateway.download_tile",
                return_value=(200, b"x", "image/png"),
            ) as redata,
        ):
            _fetch_tile("some_future_layer", 12, 1205, 1539)

        redata.assert_called_once()
        vendor.assert_not_called()

    def test_asking_the_vendor_gateway_for_a_layer_it_has_no_url_for_is_loud(self) -> None:
        """Silently falling back would hide a typo in the table behind a slower path that works."""
        with self.assertRaises(ValueError):
            BasemapVendorTilesGateway().download_tile("some_future_layer", 1, 1, 1)


class TileCoordinatesAreNotLoggedTests(SimpleTestCase):
    """`ApiCallLog` records an endpoint per call, and a tile coordinate is a location."""

    def test_the_logged_endpoint_keeps_the_service_but_drops_the_coordinate(self) -> None:
        logged = BasemapVendorTilesGateway.endpoint_for_log(VENDOR_TILES["satellite"].url_for(z=14, x=4841, y=6170))

        self.assertEqual(logged, "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/")
        for coordinate in ("4841", "6170"):
            self.assertNotIn(coordinate, logged)

    def test_a_template_with_no_tile_marker_falls_back_to_its_host_alone(self) -> None:
        """An XYZ vendor has no `/tile/` to split on, so the helper has to drop the whole path
        rather than pass it through."""
        vendor = VendorTiles(url_template="https://{s}.tile.example.test/{z}/{x}/{y}.png", subdomains=("a", "b", "c"))

        logged = BasemapVendorTilesGateway.endpoint_for_log(vendor.url_for(z=14, x=4841, y=6170))

        self.assertRegex(logged, r"^https://[abc]\.tile\.example\.test/$")
        self.assertNotIn("4841", logged)

    def test_every_vendor_url_survives_the_log_helper_without_a_coordinate(self) -> None:
        for layer, vendor in VENDOR_TILES.items():
            logged = BasemapVendorTilesGateway.endpoint_for_log(vendor.url_for(z=14, x=48411, y=61702))
            self.assertNotIn("48411", logged, layer)
            self.assertNotIn("61702", logged, layer)


class TheCreditFollowsTheBytesTests(SimpleTestCase):
    """Showing one vendor's credit over another's tiles is a licence breach, not a cosmetic bug.

    It is also silent: the map renders, the attribution control shows a plausible line, and the
    only thing wrong is that it names a company whose bytes are not on the screen.
    """

    @staticmethod
    def _sources() -> list[dict[str, object]]:
        return [
            {
                "id": "satellite",
                "name": "Satellite",
                "source_type": "raster",
                "attribution": "Esri, Maxar, Earthstar Geographics",
                "min_zoom": 0,
                "max_zoom": 19,
                "url_template": "https://redata.example.test/tiles/satellite/{z}/{x}/{y}/",
            },
            {
                "id": "terrain",
                "name": "Terrain",
                "source_type": "raster",
                "attribution": "© OpenTopoMap contributors",
                "min_zoom": 0,
                "max_zoom": 17,
                "url_template": "https://redata.example.test/tiles/terrain/{z}/{x}/{y}/",
            },
            {
                "id": "dark",
                "name": "Dark",
                "source_type": "vector",
                "attribution": "© OpenStreetMap contributors © Protomaps",
                "fallback_attribution": "© OpenStreetMap contributors © CARTO",
                "min_zoom": 0,
                "max_zoom": 15,
                "style_url": "https://tiles.example.test/styles/dark.json",
                "url_template": "https://redata.example.test/tiles/dark/{z}/{x}/{y}/",
            },
        ]

    def test_a_layer_we_fetch_from_redatas_own_vendor_keeps_redatas_credit(self) -> None:
        """Satellite is the same Esri endpoint either way, so there is nothing to override - and
        inventing a credit here would be its own licence error."""
        self.assertIsNone(VENDOR_TILES["satellite"].attribution)

        entry = next(e for e in _offered_layers(self._sources()) if e["id"] == "satellite")

        self.assertEqual(entry["attribution"], "Esri, Maxar, Earthstar Geographics")

    def test_swapping_darks_vendor_swaps_the_credit_for_its_raster_half(self) -> None:
        """`dark` draws as Protomaps vector, so the raster is the fallback - and it is
        `fallback_attribution`, not `attribution`, that describes those bytes."""
        entry = next(e for e in _offered_layers(self._sources()) if e["id"] == "dark")

        self.assertNotIn("CARTO", entry["fallback_attribution"])
        self.assertEqual(entry["fallback_attribution"], VENDOR_TILES["dark"].attribution)
        # The vector half is still Protomaps' and must not have been overwritten with the raster's.
        self.assertEqual(entry["attribution"], "© OpenStreetMap contributors © Protomaps")

    def test_every_overridden_vendor_declares_a_credit(self) -> None:
        """The table is the only place that knows the bytes changed vendor."""
        redata_vendors = {
            "street": "server.arcgisonline.com",
            "satellite": "server.arcgisonline.com",
            "borders": "server.arcgisonline.com",
            "terrain": "tile.opentopomap.org",
        }
        for layer, vendor in VENDOR_TILES.items():
            expected_host = redata_vendors.get(layer)
            if expected_host and expected_host in vendor.url_template:
                continue  # same vendor REData names; REData's credit is correct
            self.assertIsNotNone(vendor.attribution, f"{layer} changed vendor without changing its credit")


class TheDepthFollowsTheBytesTests(SimpleTestCase):
    """A vendor swap can change how deep the layer goes, and Esri does not say so with a 404.

    Past its coverage `World_Hillshade` answers 200 with a constant blank JPEG, so publishing
    REData's depth for a vendor that is shallower means the proxy fetches blanks, caches them for a
    week, and the map draws empty squares where relief should be.
    """

    def test_a_shallower_vendor_lowers_the_published_depth(self) -> None:
        entry = next(e for e in _offered_layers(TheCreditFollowsTheBytesTests._sources()) if e["id"] == "terrain")

        self.assertEqual(VENDOR_TILES["terrain"].max_native_zoom, 16)
        self.assertEqual(entry["max_zoom"], 16, "published REData's OpenTopoMap depth over Esri's bytes")

    def test_a_vendor_that_declares_no_depth_leaves_redatas_alone(self) -> None:
        """Most layers are the same endpoint REData names, so there is nothing to correct - and
        inventing a ceiling here would crop a layer that was fine."""
        self.assertIsNone(VENDOR_TILES["satellite"].max_native_zoom)

        entry = next(e for e in _offered_layers(TheCreditFollowsTheBytesTests._sources()) if e["id"] == "satellite")

        self.assertEqual(entry["max_zoom"], 19)

    def test_a_vendor_ceiling_only_ever_caps_and_never_raises(self) -> None:
        """A vendor holding tiles deeper than REData publishes says nothing about whether REData
        will serve them, so raising the published depth would offer levels the proxy may 400 on."""
        sources = [s for s in TheCreditFollowsTheBytesTests._sources() if s["id"] == "terrain"]
        sources[0]["max_zoom"] = 12

        entry = next(e for e in _offered_layers(sources) if e["id"] == "terrain")

        self.assertEqual(entry["max_zoom"], 12, "raised REData's depth to the vendor's ceiling")

    def test_on_a_vector_layer_the_depth_lands_on_the_raster_half(self) -> None:
        """`max_zoom` is the vector style's ceiling and `fallback_max_zoom` the raster's, so a
        raster vendor's limit applied to `max_zoom` would crop the layer actually being drawn."""
        shallow = VendorTiles(
            url_template=VENDOR_TILES["dark"].url_template,
            attribution=VENDOR_TILES["dark"].attribution,
            max_native_zoom=11,
        )
        with mock.patch.dict(VENDOR_TILES, {"dark": shallow}):
            entry = next(e for e in _offered_layers(TheCreditFollowsTheBytesTests._sources()) if e["id"] == "dark")

        self.assertEqual(entry["fallback_max_zoom"], 11)
        self.assertEqual(entry["max_zoom"], 15, "the Protomaps style's own depth was overwritten")


class TheVendorGatewayIsRateLimitedTests(SimpleTestCase):
    """These vendors are free and keyless; the budget is politeness, and it only applies if the key is registered."""

    def test_its_service_key_is_registered(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import SERVICE_REGISTRY

        self.assertIn(BasemapVendorTilesGateway.service_key, SERVICE_REGISTRY)
