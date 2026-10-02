"""img-src names the hosts pages load images from, and nothing wider (P165).

Third-party pictures are served from this site's own copies, so what is left is map tiles, the Maps JavaScript API's
imagery, and the images a vendor stylesheet draws from beside itself. A host missing here is a grey map for everyone, so
the hosts are read out of the code that configures them rather than listed a second time.
"""

from __future__ import annotations

from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import urlparse

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.template import Context, Template
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.inline_scripts import rendered_config
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core.vendor_assets import (
    LEAFLET_MARKER_ARTWORK,
    VENDOR_ASSETS,
    leaflet_marker_artwork,
)
from urbanlens.UrbanLens.settings.app import settings as app_settings

_DASHBOARD = Path(__file__).resolve().parents[2]
_FRONTEND = _DASHBOARD / "frontend"

#: An XYZ template written into page code: ``https://{s}.host/{z}/{x}/{y}.png`` and the like.
_TILE_TEMPLATE = re.compile(r"""https?://[^\s"'`<>]*\{z\}[^\s"'`<>]*""")

#: Hosts the Maps JavaScript API loads imagery from, one per pattern in Google's CSP guide
#: (developers.google.com/maps/documentation/javascript/content-security-policy, allowlist img-src).
_MAPS_JAVASCRIPT_API_IMAGE_HOSTS = (
    "streetviewpixels-pa.googleapis.com",
    "khms0.googleapis.com",
    "maps.gstatic.com",
    "www.google.com",
    "lh3.googleusercontent.com",
)


def _img_src() -> list[str]:
    from urbanlens.UrbanLens.settings.base import _CSP_DIRECTIVES

    return _CSP_DIRECTIVES["img-src"]


def _admits(sources: list[str], url: str) -> bool:
    """Whether a CSP source list lets the browser load *url*, for the host-source forms the policy uses."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    for source in sources:
        match = re.fullmatch(r"(?:(?P<scheme>[a-z][a-z0-9+.-]*)://)?(?P<host>\*\.[^/:*]+|[^/:*]+)(?::\d+)?/?", source)
        if match is None or (match["scheme"] and match["scheme"] != parsed.scheme):
            continue
        pattern = match["host"]
        if host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:])):
            return True
    return False


def _page_code() -> dict[Path, str]:
    """Everything a browser runs or is sent: TypeScript, hand-written scripts and templates."""
    paths = [
        path
        for path in (_FRONTEND / "ts").rglob("*.ts")
        if not path.name.endswith(".test.ts") and "testing" not in path.parts
    ]
    paths += list((_FRONTEND / "static" / "js").glob("*.js"))
    paths += list((_DASHBOARD / "templates").rglob("*.html"))
    return {path: path.read_text(encoding="utf-8") for path in paths}


class ImgSrcIsAnAllowlistTests(SimpleTestCase):
    def test_no_source_admits_every_host(self) -> None:
        for wide in ("https:", "http:", "*", "https://*", "http://*"):
            self.assertNotIn(wide, _img_src())

    def test_the_matcher_reads_host_sources_the_way_a_browser_does(self) -> None:
        self.assertTrue(_admits(["https://*.example.test"], "https://a.example.test/1.png"))
        self.assertFalse(_admits(["https://*.example.test"], "https://example.test/1.png"))
        self.assertFalse(_admits(["https://example.test"], "http://example.test/1.png"))
        self.assertFalse(_admits(["'self'", "data:"], "https://example.test/1.png"))


class EveryImageHostTheCodeConfiguresIsAdmittedTests(SimpleTestCase):
    """Read from the code, so a newly added basemap or overlay cannot be refused without this failing."""

    def test_every_tile_template_in_page_code_is_admitted(self) -> None:
        templates = {
            (path.relative_to(_DASHBOARD), template)
            for path, text in _page_code().items()
            for template in _TILE_TEMPLATE.findall(text)
        }
        from_map_layers = [template for path, template in templates if path.name == "map-layers.ts"]
        self.assertGreaterEqual(len(from_map_layers), 5, "the scan no longer finds the built-in base layers")
        for path, template in templates:
            url = template.replace("{s}", "a")
            self.assertTrue(
                _admits(_img_src(), url), f"{path} draws tiles from {urlparse(url).hostname}, refused by img-src"
            )

    def test_the_maps_javascript_api_imagery_is_admitted_where_a_page_loads_it(self) -> None:
        loaders = [path for path, text in _page_code().items() if "maps.googleapis.com/maps/api/js" in text]
        self.assertTrue(loaders, "nothing loads the Maps JavaScript API any more; its img-src hosts can go")
        for host in _MAPS_JAVASCRIPT_API_IMAGE_HOSTS:
            self.assertTrue(_admits(_img_src(), f"https://{host}/x.png"), f"{host}, which {loaders[0].name} needs")

    def test_every_vendor_stylesheet_origin_is_admitted(self) -> None:
        """A stylesheet's relative ``url()`` loads from beside it: leaflet.draw's toolbar sprite comes from its CDN."""
        styles = [asset for asset in VENDOR_ASSETS.values() if asset.kind == "style"]
        self.assertTrue(styles)
        for asset in styles:
            self.assertTrue(_admits(_img_src(), asset.fallback), f"{asset.path}'s images, from {asset.fallback}")


class ConfiguredOriginsReachImgSrcTests(SimpleTestCase):
    """Origins a deployment configures are admitted alongside the fixed list."""

    def test_a_vector_basemap_origin_is_admitted_for_its_images(self) -> None:
        """MapLibre draws a raster source as ``<img>`` once tiles stop refreshing; the sprite falls back to one too."""
        from urbanlens.UrbanLens.settings.base import allow_basemap_style_origins

        directives: dict[str, list[str]] = {"connect-src": ["'self'"], "img-src": ["'self'"], "script-src": ["'self'"]}

        allow_basemap_style_origins(
            directives, "https://tiles.example.test/styles/street.json https://glyphs.example.test"
        )

        self.assertEqual(directives["img-src"], ["'self'", "https://tiles.example.test", "https://glyphs.example.test"])
        self.assertEqual(directives["script-src"], ["'self'"])

    def test_a_vendor_mirror_is_admitted_for_its_stylesheets_images(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, list[str]] = {"img-src": ["'self'"]}

        allow_vendor_mirror(directives, "https://assets.example.test/vendor")

        self.assertEqual(directives["img-src"], ["'self'", "https://assets.example.test"])

    def test_a_media_origin_is_admitted(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_media_origin

        directives: dict[str, list[str]] = {"img-src": ["'self'"]}

        allow_media_origin(directives, "https://media.example.test")

        self.assertIn("https://media.example.test", directives["img-src"])


class LeafletMarkerArtworkIsServedFromThisSiteTests(SimpleTestCase):
    """Leaflet otherwise works its marker images' address out from leaflet.css and fetches them from that CDN."""

    def test_each_image_is_a_static_file_of_this_site(self) -> None:
        artwork = leaflet_marker_artwork()
        self.assertEqual(set(artwork), {"iconUrl", "iconRetinaUrl", "shadowUrl"})
        for name, path in LEAFLET_MARKER_ARTWORK.items():
            self.assertIsNotNone(finders.find(path), f"{path} is not a static file")
            self.assertTrue(artwork[name].startswith("/"), artwork[name])

    def test_the_artwork_matches_the_leaflet_it_is_drawn_by(self) -> None:
        library = VENDOR_ASSETS["leaflet_js"].path.split("/")[1]
        for path in LEAFLET_MARKER_ARTWORK.values():
            self.assertIn(f"/leaflet/{library}/", path)

    def test_every_page_embeds_it_for_the_default_marker(self) -> None:
        rendered = Template("{% load vendor_assets %}{% leaflet_marker_artwork %}").render(Context({}))

        self.assertIn('id="ul-leaflet-marker-artwork"', rendered)
        for url in leaflet_marker_artwork().values():
            self.assertIn(url, rendered)
        base = (_DASHBOARD / "templates" / "dashboard" / "themes" / "base.html").read_text(encoding="utf-8")
        self.assertIn("{% leaflet_marker_artwork %}", base)

    def test_a_page_can_name_one_image(self) -> None:
        rendered = Template('{% load vendor_assets %}{% leaflet_marker_url "shadowUrl" %}').render(Context({}))

        self.assertEqual(rendered, leaflet_marker_artwork()["shadowUrl"])

    def test_no_template_asks_a_cdn_for_it(self) -> None:
        for path, text in _page_code().items():
            self.assertNotRegex(text, r"leaflet[^\"'\s]*/images/marker-", str(path))


class MapPageMarkerArtworkTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        toolbar = patch.object(app_settings, "allow_dev_toolbar_for_non_admins", new=False)
        toolbar.start()
        self.addCleanup(toolbar.stop)
        baker.make(User)  # the first user is auto-promoted to site admin
        self.user = baker.make(User)
        baker.make(Pin, profile=self.user.profile, location=baker.make(Location, latitude="41.5", longitude="-71.5"))
        self.client.force_login(self.user)

    def test_the_map_page_draws_its_markers_from_this_site(self) -> None:
        response = self.client.get(reverse("map.view"))
        config = rendered_config(bytes(response.content), "map-page-config") or {}

        artwork = leaflet_marker_artwork()
        self.assertEqual(
            config.get("assets"), {"leafletMarkerIcon": artwork["iconUrl"], "leafletMarkerShadow": artwork["shadowUrl"]}
        )
        self.assertContains(response, 'id="ul-leaflet-marker-artwork"')
