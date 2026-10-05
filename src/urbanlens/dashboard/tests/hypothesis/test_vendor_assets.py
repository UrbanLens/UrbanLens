"""Third-party assets resolve to one place, chosen when the page is rendered."""

from __future__ import annotations

import json
from pathlib import Path
import re
from unittest import mock

from django.template import Context, Template
from django.test import SimpleTestCase

from urbanlens.dashboard.services.core.vendor_assets import VENDOR_ASSETS, vendor_asset_tag, vendor_asset_url

_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates"
_PACKAGE_JSON = Path(__file__).resolve().parents[5] / "package.json"
#: How a template loads the MapLibre this site builds (`entries-classic/maplibre-gl.ts`).
_MAPLIBRE_SCRIPT = "dashboard/js/maplibre-gl.js"
_CDN_HOSTS = ("cdnjs.cloudflare.com", "unpkg.com", "cdn.jsdelivr.net", "code.jquery.com")


def _mirrored(url: str):
    """Pretend an operator has configured a mirror at `url`."""
    return mock.patch("urbanlens.dashboard.services.core.vendor_assets._mirror_root", return_value=url)


class VendorAssetTableTests(SimpleTestCase):
    """The table itself."""

    def test_every_fallback_is_an_absolute_https_url(self) -> None:
        for key, asset in VENDOR_ASSETS.items():
            self.assertTrue(asset.fallback.startswith("https://"), f"{key} falls back to {asset.fallback!r}")

    def test_every_asset_pins_a_version(self) -> None:
        """An unpinned URL serves whatever the CDN publishes next.

        One template asked for `unpkg.com/leaflet/dist/leaflet.js` with no
        version at all, which is a different library on any given day.
        """
        for key, asset in VENDOR_ASSETS.items():
            self.assertRegex(asset.path, r"\d+\.\d+", f"{key}'s path names no version: {asset.path!r}")

    def test_every_script_and_style_pins_an_integrity_hash(self) -> None:
        """Every ``<script>``/``<link>`` this table can render must be checkable."""
        for key, asset in VENDOR_ASSETS.items():
            self.assertTrue(asset.integrity, f"{key} ({asset.kind}) has no integrity hash")
            self.assertRegex(
                asset.integrity,
                r"^sha(256|384|512)-",
                f"{key}'s integrity value {asset.integrity!r} is not a valid SRI hash",
            )


class VendorAssetResolutionTests(SimpleTestCase):
    """Where a given asset is loaded from, and what the tag says about it."""

    def test_without_a_mirror_the_public_url_is_used(self) -> None:
        with _mirrored(""):
            self.assertEqual(vendor_asset_url("leaflet_js"), VENDOR_ASSETS["leaflet_js"].fallback)

    def test_with_a_mirror_every_asset_comes_from_it(self) -> None:
        with _mirrored("https://assets.example.test/vendor"):
            for key, asset in VENDOR_ASSETS.items():
                self.assertEqual(vendor_asset_url(key), f"https://assets.example.test/vendor/{asset.path}", key)

    def test_a_trailing_slash_on_the_mirror_does_not_double_up(self) -> None:
        with _mirrored("https://assets.example.test/vendor/"):
            self.assertNotIn("//vendor", vendor_asset_url("toastr_js").removeprefix("https://"))

    def test_integrity_is_claimed_only_for_the_bytes_it_describes(self) -> None:
        """An SRI hash describes the CDN's copy. A mirror serving a re-minified
        or re-compressed file would fail that check and drop the script."""
        with _mirrored(""):
            self.assertIn("integrity=", vendor_asset_tag("toastr_js"))
        with _mirrored("https://assets.example.test/vendor"):
            self.assertNotIn("integrity=", vendor_asset_tag("toastr_js"))

    def test_a_stylesheet_and_a_script_render_their_own_tag(self) -> None:
        with _mirrored(""):
            self.assertIn('<link rel="stylesheet"', vendor_asset_tag("leaflet_css"))
            self.assertIn("<script src=", vendor_asset_tag("leaflet_js"))

    def test_an_unknown_asset_fails_the_render_rather_than_writing_nothing(self) -> None:
        """A silently missing script is a broken page with no explanation."""
        with self.assertRaises(KeyError):
            vendor_asset_url("leaflet_js_typo")

    def test_the_template_tag_renders(self) -> None:
        with _mirrored("https://assets.example.test/vendor"):
            rendered = Template('{% load vendor_assets %}{% vendor_asset "leaflet_js" %}').render(Context({}))
        self.assertIn("https://assets.example.test/vendor/leaflet/1.9.4/leaflet.js", rendered)


class NoRawCdnUrlsInTemplatesTests(SimpleTestCase):
    """The structural half: the table is only single-source while it is the only source."""

    def test_no_template_names_a_cdn_directly(self) -> None:
        offenders: list[str] = []
        pattern = re.compile("|".join(re.escape(host) for host in _CDN_HOSTS))
        for path in _TEMPLATE_ROOT.rglob("*.html"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(_TEMPLATE_ROOT)}:{number}: {line.strip()[:100]}")
        self.assertEqual(
            offenders, [], "add the asset to VENDOR_ASSETS and use {% vendor_asset %}:\n" + "\n".join(offenders)
        )


class MaplibreIsOneReleaseTests(SimpleTestCase):
    """MapLibre's script is built from package.json's release; its stylesheet comes from the CDN, so the two can drift."""

    def test_the_stylesheet_is_the_release_the_script_is_built_from(self) -> None:
        declared = json.loads(_PACKAGE_JSON.read_text(encoding="utf-8"))["dependencies"]["maplibre-gl"]
        # A range would let `bun install` move the script to a release the pinned stylesheet does not match.
        self.assertRegex(declared, r"^\d+\.\d+\.\d+$", "pin maplibre-gl to an exact version in package.json")

        stylesheet = VENDOR_ASSETS["maplibregl_css"]
        self.assertEqual(stylesheet.path, f"maplibre-gl/{declared}/maplibre-gl.css")
        self.assertEqual(stylesheet.fallback, f"https://unpkg.com/maplibre-gl@{declared}/dist/maplibre-gl.css")


class MaplibreLoadOrderTests(SimpleTestCase):
    """Which MapLibre files a page loads, and in what order."""

    @staticmethod
    def _templates() -> list[tuple[str, str]]:
        return [
            (str(path.relative_to(_TEMPLATE_ROOT)), path.read_text(encoding="utf-8"))
            for path in _TEMPLATE_ROOT.rglob("*.html")
        ]

    def test_a_page_with_the_stylesheet_has_the_script_and_the_reverse(self) -> None:
        unpaired = [
            name
            for name, text in self._templates()
            if ('vendor_asset "maplibregl_css"' in text) != (_MAPLIBRE_SCRIPT in text)
        ]
        self.assertEqual(unpaired, [])

    def test_the_leaflet_bridge_follows_leaflet_and_maplibre(self) -> None:
        """The bridge reads `L` and `maplibregl` once, as it runs, and draws nothing if either was missing then."""
        bridged = 0
        misordered: list[str] = []
        for name, text in self._templates():
            bridge = text.find('vendor_asset "maplibregl_leaflet_js"')
            if bridge < 0:
                continue
            bridged += 1
            if (
                not 0 <= text.find('vendor_asset "leaflet_js"') < bridge
                or not 0 <= text.find(_MAPLIBRE_SCRIPT) < bridge
            ):
                misordered.append(name)
        self.assertGreater(bridged, 0, "no template loads the bridge - has its asset key changed?")
        self.assertEqual(misordered, [])


class VendorSourceMapsAreAllowedByThePolicyTests(SimpleTestCase):
    """Devtools fetches a script's source map from beside it under connect-src, so a script from an origin that omits logs a CSP violation."""

    def test_every_script_comes_from_an_origin_connect_src_admits(self) -> None:
        from urllib.parse import urlparse

        from urbanlens.UrbanLens.settings.base import _CSP_DIRECTIVES

        for key, asset in VENDOR_ASSETS.items():
            if asset.kind != "script":
                continue
            parsed = urlparse(asset.fallback)
            self.assertIn(f"{parsed.scheme}://{parsed.netloc}", _CSP_DIRECTIVES["connect-src"], key)

    def test_script_src_admits_no_cdn_that_serves_no_vendor_script(self) -> None:
        from urllib.parse import urlparse

        from urbanlens.UrbanLens.settings.base import _CSP_DIRECTIVES

        used = {
            f"{urlparse(asset.fallback).scheme}://{urlparse(asset.fallback).netloc}"
            for asset in VENDOR_ASSETS.values()
            if asset.kind == "script"
        }
        for host in _CDN_HOSTS:
            if f"https://{host}" not in used:
                self.assertNotIn(f"https://{host}", _CSP_DIRECTIVES["script-src"], host)

    def test_a_mirror_is_admitted_to_connect_src_for_the_same_reason(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, list[str]] = {
            "script-src": ["'self'"],
            "style-src": [],
            "font-src": [],
            "connect-src": ["'self'"],
        }

        allow_vendor_mirror(directives, "https://assets.example.test/vendor")

        self.assertIn("https://assets.example.test", directives["connect-src"])


class VendorMirrorIsAllowedByThePolicyTests(SimpleTestCase):
    """A mirror the policy does not admit is every asset gone, not some."""

    def test_the_mirror_origin_reaches_the_directives_that_serve_it(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, list[str]] = {
            "script-src": ["'self'"],
            "style-src": ["'self'"],
            "font-src": ["'self'"],
            "img-src": ["'self'"],
        }

        origin = allow_vendor_mirror(directives, "https://assets.example.test/vendor/leaflet")

        self.assertEqual(origin, "https://assets.example.test")
        # img-src: a mirrored stylesheet draws its images (leaflet.draw's toolbar sprite) from beside itself.
        for name in ("script-src", "style-src", "font-src", "img-src"):
            self.assertIn("https://assets.example.test", directives[name], name)

    def test_no_mirror_configured_changes_nothing(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, list[str]] = {"script-src": ["'self'"]}

        self.assertIsNone(allow_vendor_mirror(directives, None))
        self.assertEqual(directives["script-src"], ["'self'"])

    def test_the_origin_is_admitted_once_however_deep_the_root(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, list[str]] = {"script-src": ["'self'"], "style-src": [], "font-src": []}

        allow_vendor_mirror(directives, "https://assets.example.test/a/b/c")
        allow_vendor_mirror(directives, "https://assets.example.test/d")

        self.assertEqual(directives["script-src"].count("https://assets.example.test"), 1)


class BasemapStyleOriginIsAllowedByThePolicyTests(SimpleTestCase):
    """A self-hosted vector basemap is the one map layer the browser fetches itself.

    A raster layer is proxied, so the browser only ever talks to this origin and the policy needs
    no exception. A vector layer is the opposite: REData publishes a ``style_url`` and the browser
    goes straight to it for the style, its glyphs, its sprite and the PMTiles archive. Refused by
    CSP, the map is blank with no network error a user could act on.
    """

    def test_the_style_origin_reaches_connect_src_and_img_src_only(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_basemap_style_origins

        directives: dict[str, list[str]] = {
            "connect-src": ["'self'"],
            "img-src": ["'self'"],
            "script-src": ["'self'"],
        }

        origins = allow_basemap_style_origins(directives, "https://tiles.example.test/styles/street.json")

        self.assertEqual(origins, ["https://tiles.example.test"])
        # img-src: MapLibre draws a raster source the style names as <img>. script-src must not widen
        # for a style document, which is data and never executes.
        self.assertIn("https://tiles.example.test", directives["connect-src"])
        self.assertIn("https://tiles.example.test", directives["img-src"])
        self.assertNotIn("https://tiles.example.test", directives["script-src"])

    def test_the_real_policy_declares_the_directives_this_helper_writes_to(self) -> None:
        """The helper appends only to lists that already exist, so naming a directive the policy does not declare is a silent no-op that reads like configuration - which `worker-src` was, before this caught it."""
        from urbanlens.UrbanLens.settings.base import _CSP_DIRECTIVES

        self.assertIsInstance(_CSP_DIRECTIVES.get("connect-src"), list)
        self.assertIsInstance(_CSP_DIRECTIVES.get("img-src"), list)

    def test_buying_the_hosted_basemap_admits_its_glyph_host(self) -> None:
        """The proxied style still names protomaps.github.io for glyphs and sprites; unadmitted, the map has no labels."""
        from urbanlens.UrbanLens.settings.base import allow_hosted_basemap_assets

        directives: dict[str, list[str]] = {"connect-src": ["'self'"], "script-src": ["'self'"]}

        self.assertEqual(allow_hosted_basemap_assets(directives, ""), [])
        self.assertEqual(directives["connect-src"], ["'self'"])

        allow_hosted_basemap_assets(directives, "pk_test")
        self.assertEqual(directives["connect-src"], ["'self'", "https://protomaps.github.io"])
        self.assertEqual(directives["script-src"], ["'self'"])

    def test_no_style_origin_configured_changes_nothing(self) -> None:
        """The default for every deployment today, hosted and self-hosted: REData offers only raster."""
        from urbanlens.UrbanLens.settings.base import allow_basemap_style_origins

        directives: dict[str, list[str]] = {"connect-src": ["'self'"]}

        self.assertEqual(allow_basemap_style_origins(directives, ""), [])
        self.assertEqual(directives["connect-src"], ["'self'"])

    def test_the_origin_is_admitted_once_however_deep_the_url(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_basemap_style_origins

        directives: dict[str, list[str]] = {"connect-src": []}

        allow_basemap_style_origins(directives, "https://tiles.example.test/a/b/style.json")
        allow_basemap_style_origins(directives, "https://tiles.example.test/c/d/other.json")

        self.assertEqual(directives["connect-src"], ["https://tiles.example.test"])

    def test_a_style_whose_assets_live_on_another_host_admits_both(self) -> None:
        """Protomaps' hosted API serves tiles from one host and the glyphs and sprite from another; admitting only the first leaves MapLibre with no labels."""
        from urbanlens.UrbanLens.settings.base import allow_basemap_style_origins

        directives: dict[str, list[str]] = {"connect-src": ["'self'"]}

        origins = allow_basemap_style_origins(directives, "https://api.protomaps.com https://protomaps.github.io")

        self.assertEqual(origins, ["https://api.protomaps.com", "https://protomaps.github.io"])
        self.assertEqual(
            directives["connect-src"], ["'self'", "https://api.protomaps.com", "https://protomaps.github.io"]
        )

    def test_a_value_that_is_not_a_url_is_skipped_rather_than_admitted(self) -> None:
        """A bare hostname has no scheme, and `scheme://` with an empty netloc is not an origin - either would widen connect-src with a value no browser matches."""
        from urbanlens.UrbanLens.settings.base import allow_basemap_style_origins

        directives: dict[str, list[str]] = {"connect-src": []}

        self.assertEqual(
            allow_basemap_style_origins(directives, "tiles.example.test , https://ok.example.test"),
            ["https://ok.example.test"],
        )
        self.assertEqual(directives["connect-src"], ["https://ok.example.test"])
