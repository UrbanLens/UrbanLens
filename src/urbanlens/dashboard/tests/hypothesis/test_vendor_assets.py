"""Third-party assets resolve to one place, chosen when the page is rendered.

Every script and stylesheet the site loads from someone else's server used to be
written out inline in whichever template wanted it. That is how leaflet-draw came
to be requested from two different CDNs, how one template asked unpkg for
whatever Leaflet it happened to be serving that day, and how Leaflet's marker
images came to be fetched from a different release than the library using them.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

from django.template import Context, Template
from django.test import SimpleTestCase

from urbanlens.dashboard.services.core.vendor_assets import VENDOR_ASSETS, vendor_asset_tag, vendor_asset_url

_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "templates"
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

    def test_leaflet_artwork_matches_the_leaflet_it_is_drawn_by(self) -> None:
        """The marker images were served from 1.7.1 while the library was 1.9.4."""
        library = VENDOR_ASSETS["leaflet_js"].path.split("/")[1]
        for key in ("leaflet_marker_icon", "leaflet_marker_shadow"):
            self.assertEqual(VENDOR_ASSETS[key].path.split("/")[1], library, f"{key} is from a different Leaflet release")


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
            self.assertIn("<link rel=\"stylesheet\"", vendor_asset_tag("leaflet_css"))
            self.assertIn("<script src=", vendor_asset_tag("leaflet_js"))

    def test_an_unknown_asset_fails_the_render_rather_than_writing_nothing(self) -> None:
        """A silently missing script is a broken page with no explanation."""
        with self.assertRaises(KeyError):
            vendor_asset_url("leaflet_js_typo")

    def test_an_image_has_no_tag_of_its_own(self) -> None:
        with self.assertRaises(ValueError):
            vendor_asset_tag("leaflet_marker_icon")

    def test_the_template_tags_render(self) -> None:
        with _mirrored("https://assets.example.test/vendor"):
            rendered = Template('{% load vendor_assets %}{% vendor_asset "leaflet_js" %}|{% vendor_asset_source "leaflet_marker_icon" %}').render(Context({}))
        tag, url = rendered.split("|")
        self.assertIn("https://assets.example.test/vendor/leaflet/1.9.4/leaflet.js", tag)
        self.assertEqual(url, "https://assets.example.test/vendor/leaflet/1.9.4/images/marker-icon.png")


class NoRawCdnUrlsInTemplatesTests(SimpleTestCase):
    """The structural half: the table is only single-source while it is the only source."""

    def test_no_template_names_a_cdn_directly(self) -> None:
        offenders: list[str] = []
        pattern = re.compile("|".join(re.escape(host) for host in _CDN_HOSTS))
        for path in _TEMPLATE_ROOT.rglob("*.html"):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(_TEMPLATE_ROOT)}:{number}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "add the asset to VENDOR_ASSETS and use {% vendor_asset %}:\n" + "\n".join(offenders))


class VendorMirrorIsAllowedByThePolicyTests(SimpleTestCase):
    """A mirror the policy does not admit is every asset gone, not some."""

    def test_the_mirror_origin_reaches_the_directives_that_serve_it(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, object] = {"script-src": ["'self'"], "style-src": ["'self'"], "font-src": ["'self'"], "img-src": ["https:"]}

        origin = allow_vendor_mirror(directives, "https://assets.example.test/vendor/leaflet")

        self.assertEqual(origin, "https://assets.example.test")
        for name in ("script-src", "style-src", "font-src"):
            self.assertIn("https://assets.example.test", directives[name], name)
        # img-src already allows https: wholesale; adding a host would be noise.
        self.assertNotIn("https://assets.example.test", directives["img-src"])

    def test_no_mirror_configured_changes_nothing(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, object] = {"script-src": ["'self'"]}

        self.assertIsNone(allow_vendor_mirror(directives, None))
        self.assertEqual(directives["script-src"], ["'self'"])

    def test_the_origin_is_admitted_once_however_deep_the_root(self) -> None:
        from urbanlens.UrbanLens.settings.base import allow_vendor_mirror

        directives: dict[str, object] = {"script-src": ["'self'"], "style-src": [], "font-src": []}

        allow_vendor_mirror(directives, "https://assets.example.test/a/b/c")
        allow_vendor_mirror(directives, "https://assets.example.test/d")

        self.assertEqual(directives["script-src"].count("https://assets.example.test"), 1)
