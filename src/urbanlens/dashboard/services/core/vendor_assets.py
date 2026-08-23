"""One table of every third-party asset the site loads, and where to load it from.

Third-party scripts and stylesheets were written inline in each template that
wanted them, which is how the same library came to be requested from two
different CDNs, one library came to be pinned in most places and unpinned in one,
and Leaflet's marker images came to be served from a different release of Leaflet
than the library itself. A table makes a version a property of the asset rather
than of each of the twenty-seven templates that mention it.

It also decides *where* an asset comes from, once, at render time. An instance
that mirrors these files sets ``UL_VENDOR_ASSET_BASE_URL`` and every tag points
there; an instance that sets nothing keeps loading from the public CDNs exactly
as before. The choice is made when the page is built, so nothing branches at call
time and nothing waits for a request to fail before trying somewhere else - a
failover would mean the page has already paid for the timeout.

The mirrored files deliberately do not live in this repository: they are other
projects' releases, with their own licences, and vendoring them into an
open-source application is a redistribution decision this project has not made.
``UL_VENDOR_ASSET_BASE_URL`` points at wherever an operator has put them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from django.utils.html import format_html

if TYPE_CHECKING:
    from django.utils.safestring import SafeString


@dataclass(frozen=True)
class VendorAsset:
    """A third-party file the site loads.

    Attributes:
        kind: What tag renders it. ``image`` assets have no tag; they are
            referenced by URL from script and CSS.
        path: Where the file sits under the mirror root, when one is configured.
            Also the identity of the version: change it and both sources move
            together.
        fallback: The public URL used when no mirror is configured.
        integrity: Subresource-integrity hash for ``fallback`` only. A mirror
            serving a re-compressed or differently-minified copy would fail an
            SRI check against the CDN's bytes, so this is not emitted for a
            mirrored asset - which is same-origin and covered by the operator
            controlling it.
    """

    kind: Literal["script", "style", "image"]
    path: str
    fallback: str
    integrity: str = ""


#: Every third-party asset, keyed by the name templates ask for.
VENDOR_ASSETS: dict[str, VendorAsset] = {
    "leaflet_css": VendorAsset("style", "leaflet/1.9.4/leaflet.css", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"),
    "leaflet_js": VendorAsset("script", "leaflet/1.9.4/leaflet.js", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"),
    # Leaflet's own default marker artwork. Previously requested from Leaflet
    # 1.7.1 while the library was 1.9.4.
    "leaflet_marker_icon": VendorAsset("image", "leaflet/1.9.4/images/marker-icon.png", "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png"),
    "leaflet_marker_shadow": VendorAsset("image", "leaflet/1.9.4/images/marker-shadow.png", "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png"),
    "leaflet_rotate_js": VendorAsset("script", "leaflet-rotate/0.2.8/leaflet-rotate-src.js", "https://unpkg.com/leaflet-rotate@0.2.8/dist/leaflet-rotate-src.js"),
    # Requested from cdnjs in some templates and unpkg in others; one source now.
    "leaflet_draw_css": VendorAsset("style", "leaflet-draw/1.0.4/leaflet.draw.css", "https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"),
    "leaflet_draw_js": VendorAsset("script", "leaflet-draw/1.0.4/leaflet.draw.js", "https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"),
    "leaflet_markercluster_css": VendorAsset("style", "leaflet.markercluster/1.5.3/MarkerCluster.css", "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"),
    "leaflet_markercluster_default_css": VendorAsset("style", "leaflet.markercluster/1.5.3/MarkerCluster.Default.css", "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"),
    "leaflet_markercluster_js": VendorAsset("script", "leaflet.markercluster/1.5.3/leaflet.markercluster.js", "https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"),
    "toastr_css": VendorAsset("style", "toastr/2.1.4/toastr.min.css", "https://cdnjs.cloudflare.com/ajax/libs/toastr.js/2.1.4/toastr.min.css"),
    "toastr_js": VendorAsset(
        "script",
        "toastr/2.1.4/toastr.min.js",
        "https://cdnjs.cloudflare.com/ajax/libs/toastr.js/2.1.4/toastr.min.js",
        "sha384-VDls8ImYGI8SwVxpmjX2Bn27U2TcNodzTNROTusVEWO55+lmL+H9NczoQJk6mwZR",
    ),
    "htmx_js": VendorAsset("script", "htmx/1.9.11/htmx.min.js", "https://unpkg.com/htmx.org@1.9.11"),
    "jquery_js": VendorAsset(
        "script",
        "jquery/4.0.0-beta/jquery.min.js",
        "https://code.jquery.com/jquery-4.0.0-beta.min.js",
        "sha384-Cm3jMWwIyV0dazzpp3V+n5HmonAQ2uoNpQCYQzGrAK1ZBIKjGgaiHq2N8ItUlBcJ",
    ),
    "chartjs_js": VendorAsset("script", "chart.js/4.4.0/chart.umd.min.js", "https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"),
    "sortable_js": VendorAsset("script", "sortablejs/1.15.0/Sortable.min.js", "https://cdn.jsdelivr.net/npm/sortablejs@1.15.0/Sortable.min.js"),
    "fontawesome_css": VendorAsset("style", "font-awesome/6.6.0/css/all.min.css", "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css"),
}


def _mirror_root() -> str:
    """The configured mirror root with no trailing slash, or "" when unset."""
    from urbanlens.UrbanLens.settings.app import AppSettings

    configured = getattr(AppSettings(), "vendor_asset_base_url", None)
    return str(configured).rstrip("/") if configured else ""


def vendor_asset_url(key: str) -> str:
    """Where a named asset should be loaded from.

    Args:
        key: A key of :data:`VENDOR_ASSETS`.

    Returns:
        The mirror URL when one is configured, else the public fallback.

    Raises:
        KeyError: If the key is not in the table. Raised rather than returning ""
            so a typo in a template is a failed render rather than a silently
            missing script - which reads as a broken page with no explanation.
    """
    asset = VENDOR_ASSETS[key]
    root = _mirror_root()
    if not root:
        return asset.fallback
    return f"{root}/{quote(asset.path)}"


def vendor_asset_tag(key: str) -> SafeString:
    """The ``<script>`` or ``<link>`` for a named asset.

    Args:
        key: A key of :data:`VENDOR_ASSETS`.

    Returns:
        The tag, with integrity and crossorigin only when loading from the
        public fallback.

    Raises:
        KeyError: If the key is not in the table.
        ValueError: If the asset is an image, which has no tag of its own.
    """
    asset = VENDOR_ASSETS[key]
    if asset.kind == "image":
        raise ValueError(f"{key} is an image; use vendor_asset_url")
    url = vendor_asset_url(key)
    mirrored = url != asset.fallback
    if asset.kind == "style":
        if asset.integrity and not mirrored:
            return format_html('<link rel="stylesheet" href="{}" integrity="{}" crossorigin="anonymous">', url, asset.integrity)
        return format_html('<link rel="stylesheet" href="{}">', url)
    if asset.integrity and not mirrored:
        return format_html('<script src="{}" integrity="{}" crossorigin="anonymous"></script>', url, asset.integrity)
    return format_html('<script src="{}"></script>', url)
