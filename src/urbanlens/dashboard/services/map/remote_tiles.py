"""Map tiles from another host, drawn from this site's kept copies (Jess, 2026-09-30, item 16 of the rulings).

An overlay given a foreign XYZ template draws through :func:`kept_tile_template` instead. The first request for a tile
downloads it, the sandbox worker re-encodes it (``render_remote_tile``), and every later request is served from the
stored file, whether or not the host still has it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import re
from urllib.parse import urlsplit

from django.core.files.base import ContentFile
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from urbanlens.dashboard.models.remote_tiles.model import RemoteTile, RemoteTileSource
from urbanlens.dashboard.services.core.user_agent import USER_AGENT

#: Deepest zoom an overlay asks its source for (``maxNativeZoom`` in ``map-image-overlays.ts``).
MAX_TILE_ZOOM = 19

#: Largest tile accepted from a host.
MAX_TILE_SOURCE_BYTES = 4 * 1024 * 1024

#: Longest edge of a kept tile: a retina tile is 512px.
REMOTE_TILE_MAX_DIMENSION = 512

#: Tiles kept per template before further ones are refused, so a whole-world template cannot fill the media volume.
MAX_KEPT_TILES_PER_SOURCE = 50_000

#: The first retry after a failed download; each further failure doubles it, up to :data:`MAX_RETRY_DELAY`.
FIRST_RETRY_DELAY = timedelta(hours=1)
MAX_RETRY_DELAY = timedelta(days=7)

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_REQUIRED = ("z", "x", "y")
_OPTIONAL = {"s": "a", "r": ""}
_TIMEOUT = 15


def template_digest(template: str) -> str:
    """The key a foreign template's tiles are kept under."""
    return hashlib.sha256(template.encode()).hexdigest()


def tile_url(template: str, z: int, x: int, y: int) -> str:
    """One tile's address at its host.

    Args:
        template: The XYZ template.
        z: Zoom.
        x: Column.
        y: Row.

    Returns:
        The address, with a subdomain placeholder filled with its first letter and a retina one left empty.
    """
    values = {"z": str(z), "x": str(x), "y": str(y), **_OPTIONAL}
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def is_foreign_template(template: str) -> bool:
    """Whether *template* is an XYZ tile template on a public http(s) host.

    Args:
        template: The candidate.

    Returns:
        True when it names each of ``{z}``, ``{x}`` and ``{y}`` once, no placeholder beyond ``{s}`` and ``{r}``, and a
        host that resolves to a public address.
    """
    from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, ensure_public_http_url

    if not template or len(template) > 500:
        return False
    names = _PLACEHOLDER.findall(template)
    if sorted(name for name in names if name in _REQUIRED) != sorted(_REQUIRED) or any(name not in (*_REQUIRED, *_OPTIONAL) for name in names):
        return False
    if len(names) != len(set(names)):
        return False
    sample = tile_url(template, 0, 0, 0)
    if urlsplit(sample).scheme not in ("http", "https"):
        return False
    try:
        ensure_public_http_url(sample)
    except UnsafeUrlError:
        return False
    return True


def _route_template(digest: str) -> str:
    return reverse("map.remote_tiles", args=[digest, 0, 0, 0]).replace("/0/0/0.png", "/{z}/{x}/{y}.png")


def kept_tile_template(template: str, *, provider: str) -> str:
    """This site's template for a foreign one, recording the foreign one as a source.

    Args:
        template: The foreign XYZ template, already checked with :func:`is_foreign_template`.
        provider: Where the template came from.

    Returns:
        A path-relative template on this site.
    """
    digest = template_digest(template)
    RemoteTileSource.objects.get_or_create(template_digest=digest, defaults={"template": template, "provider": provider[:64]})
    return _route_template(digest)


def source_for_route_template(template: str) -> RemoteTileSource | None:
    """The source behind one of this site's kept-tile templates.

    Args:
        template: A template, possibly one :func:`kept_tile_template` returned.

    Returns:
        Its source, or None when *template* is not such a template or names a source this site does not have.
    """
    sentinel = "0" * 64
    prefix, _, suffix = _route_template(sentinel).partition(sentinel)
    path = urlsplit(template).path if "://" in template else template
    if not (path.startswith(prefix) and path.endswith(suffix)):
        return None
    digest = path[len(prefix) : len(path) - len(suffix)]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return RemoteTileSource.objects.filter(template_digest=digest).first()


def coordinate_is_valid(z: int, x: int, y: int) -> bool:
    """Whether ``z/x/y`` names a tile an overlay can ask for.

    Args:
        z: Zoom.
        x: Column.
        y: Row.

    Returns:
        True inside the zoom range and the grid at that zoom.
    """
    return 0 <= z <= MAX_TILE_ZOOM and 0 <= x < 2**z and 0 <= y < 2**z


def pending_marker(digest: str, z: int, x: int, y: int) -> str:
    """Cache key standing while a tile's download or render is under way, so concurrent requests fetch it once."""
    return f"ul_remote_tile_{digest}_{z}_{x}_{y}"


def retry_is_due(tile: RemoteTile) -> bool:
    """Whether a tile whose downloads failed may be tried again yet.

    Args:
        tile: The tile.

    Returns:
        True when it has never failed, or its backoff has run out.
    """
    if not tile.failed_attempts or tile.last_failed_at is None:
        return True
    delay = min(FIRST_RETRY_DELAY * 2 ** (tile.failed_attempts - 1), MAX_RETRY_DELAY)
    return timezone.now() - tile.last_failed_at >= delay


def record_failure(tile: RemoteTile) -> None:
    """Count one failed download or render of *tile*."""
    RemoteTile.objects.filter(pk=tile.pk).update(failed_attempts=F("failed_attempts") + 1, last_failed_at=timezone.now())


def record_absent(tile: RemoteTile) -> None:
    """Remember that the host has no tile at *tile*'s coordinate."""
    RemoteTile.objects.filter(pk=tile.pk).update(absent=True, fetched_at=timezone.now())


def store(tile: RemoteTile, content: bytes, content_type: str) -> bool:
    """Keep a rendered tile.

    Args:
        tile: The tile.
        content: The re-encoded image.
        content_type: Its type.

    Returns:
        False when its source already keeps as many tiles as it may, and nothing was stored.
    """
    if not RemoteTileSource.objects.filter(pk=tile.source_id, kept_tiles__lt=MAX_KEPT_TILES_PER_SOURCE).update(kept_tiles=F("kept_tiles") + 1):
        return False
    extension = "png" if content_type == "image/png" else "jpg"
    tile.file.save(f"{tile.y}.{extension}", ContentFile(content), save=False)
    tile.content_type = content_type
    tile.fetched_at = timezone.now()
    tile.save(update_fields=["file", "content_type", "fetched_at", "updated"])
    return True


@dataclass(frozen=True, slots=True)
class TileDownload:
    """What a host answered for one tile.

    Attributes:
        status: The HTTP status.
        body: The body when the status was 200, else empty.
        content_type: The declared type.
    """

    status: int
    body: bytes = b""
    content_type: str = ""


def download_tile(url: str) -> TileDownload | None:
    """Download one tile, pinning each hop to the address it validated to.

    Args:
        url: The tile's address at its host.

    Returns:
        The answer, or None when the address was unsafe, the fetch failed, or the body was over the size cap.
    """
    import requests

    from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, fetch_public_url, read_limited

    try:
        response = fetch_public_url(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*"}, timeout=_TIMEOUT)
    except (UnsafeUrlError, requests.RequestException):
        return None
    with response:
        if response.status_code != 200:
            return TileDownload(response.status_code)
        try:
            body = read_limited(response, max_bytes=MAX_TILE_SOURCE_BYTES)
        except requests.RequestException:
            return None
        return TileDownload(200, body, response.headers.get("Content-Type", ""))
