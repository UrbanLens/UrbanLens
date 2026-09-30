"""Creating georeferenced image overlays, for every entry point that makes one.

The manage-overlays form, the historical-map picker and the archive importer all create overlays; each goes
through :func:`create_overlay` so the per-map cap and the "download, never reference" rule for a pasted URL
cannot be skipped by a new caller.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
import uuid

from django.db import transaction

from urbanlens.dashboard.models.map_overlay.model import MapImageOverlay
from urbanlens.dashboard.models.pin.model import Pin

if TYPE_CHECKING:
    from collections.abc import Sequence

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.map_overlay.queryset import MapImageOverlayQuerySet
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Overlays one pin's or wiki's map may hold (rendering budget).
MAX_OVERLAYS_PER_MAP = 12

#: Longest pasted image URL accepted, matching the form's ``maxlength``.
MAX_OVERLAY_URL_LENGTH = 1000


class OverlayError(ValueError):
    """An overlay could not be created; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OverlayLimitError(OverlayError):
    """The map already holds :data:`MAX_OVERLAYS_PER_MAP` overlays."""

    def __init__(self) -> None:
        super().__init__(f"A map can hold at most {MAX_OVERLAYS_PER_MAP} image overlays.")


class OverlayImageError(OverlayError):
    """A pasted image URL was refused or could not be downloaded."""


def owner_kwargs(owner: Pin | Wiki) -> dict[str, Pin | Wiki]:
    """The parent FK kwargs (``parent_pin``/``parent_wiki``) for *owner*."""
    return {"parent_pin": owner} if isinstance(owner, Pin) else {"parent_wiki": owner}


def overlays_on(owner: Pin | Wiki) -> MapImageOverlayQuerySet:
    """Every overlay on *owner*'s map, whoever created it and whoever is asking."""
    return MapImageOverlay.objects.filter(**owner_kwargs(owner))


def at_overlay_limit(owner: Pin | Wiki) -> bool:
    """Whether *owner*'s map is already full."""
    return overlays_on(owner).count() >= MAX_OVERLAYS_PER_MAP


def historical_tile_template(georeference_uuid: str) -> str:
    """The XYZ template for one REData georeference, served through this site's own tile proxy.

    ``reverse()`` can't emit literal ``{z}/{x}/{y}``, so it builds with zeros and substitutes, keeping the template
    tied to URL routing rather than a hardcoded prefix.
    """
    from django.urls import reverse

    return reverse("map.historical_tiles", args=[georeference_uuid, 0, 0, 0]).replace("/0/0/0.png", "/{z}/{x}/{y}.png")


#: REData's own tile pyramid for a georeference (``RedataHistoricalMapsGateway.download_tile``).
_REDATA_SHEET_TILES = re.compile(r"/api/v1/maps/georeferences/(?P<uuid>[0-9a-fA-F-]{36})/tiles/\{z\}/\{x\}/\{y\}\.png")


def recognized_sheet_template(template: str) -> str | None:
    """This site's template for a georeferenced sheet named by another address for it.

    Recognised: this site's own historical-tile route, relative or on any host (another deployment's export), and
    REData's own tile URL for the georeference.

    Args:
        template: An XYZ template.

    Returns:
        The sheet's template on this site, or None when *template* names no sheet this site can draw itself.
    """
    parts = urlsplit(template)
    if parts.query or parts.fragment or (parts.netloc and parts.scheme not in ("http", "https")):
        return None
    sentinel = "00000000-0000-0000-0000-000000000000"
    prefix, _, suffix = historical_tile_template(sentinel).partition(sentinel)
    if parts.path.startswith(prefix) and parts.path.endswith(suffix):
        candidate = parts.path[len(prefix) : len(parts.path) - len(suffix)]
    elif match := _REDATA_SHEET_TILES.fullmatch(parts.path):
        candidate = match["uuid"]
    else:
        return None
    try:
        return historical_tile_template(str(uuid.UUID(candidate)))
    except ValueError:
        return None


def imported_tile_template(template: str) -> str:
    """What an imported overlay's tile template becomes on this site.

    A recognised sheet is rebuilt onto this site's own route. Any other public host's tiles are drawn through this
    site and kept (:mod:`~urbanlens.dashboard.services.map.remote_tiles`), so no viewer's browser is sent to it.

    Args:
        template: The archive's template.

    Returns:
        The template to store, or ``""`` when *template* is not one this site can draw.
    """
    from urbanlens.dashboard.services.map.remote_tiles import is_foreign_template, kept_tile_template, source_for_route_template

    if rebuilt := recognized_sheet_template(template):
        return rebuilt
    if (source := source_for_route_template(template)) is not None:
        return kept_tile_template(source.template, provider=source.provider)
    if is_foreign_template(template):
        return kept_tile_template(template, provider="import")
    return ""


def exported_tile_template(template: str) -> str:
    """The template an archive carries for an overlay: a kept host's own, so another deployment can keep it too.

    Args:
        template: The overlay's template on this site.

    Returns:
        The template to export.
    """
    from urbanlens.dashboard.services.map.remote_tiles import source_for_route_template

    source = source_for_route_template(template)
    return source.template if source is not None else template


def image_from_external_url(owner: Pin | Wiki, profile: Profile, url: str, *, caption: str = "") -> Image:
    """Download a pasted image URL into an ``Image`` the overlay can own.

    A referenced URL would send every viewer's browser, and so their IP address, to the foreign host, and its
    bytes would skip the upload pipeline that re-encodes every stored photo.

    Args:
        owner: The Pin or Wiki the overlay will belong to; the image is attached to it too.
        profile: Who pays the storage quota and owns the image.
        url: The pasted URL.
        caption: Caption for the stored image.

    Returns:
        The stored (or reused) image.

    Raises:
        OverlayImageError: The URL is unsafe to fetch, not a browser-displayable image, or the download failed.
    """
    from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item
    from urbanlens.dashboard.services.media.previews import is_web_safe
    from urbanlens.dashboard.services.security.url_safety import UnsafeUrlError, ensure_public_http_url

    try:
        ensure_public_http_url(url, max_length=MAX_OVERLAY_URL_LENGTH)
    except UnsafeUrlError as exc:
        logger.info("overlay external url rejected: %s", exc)
        raise OverlayImageError("That link can't be used for an overlay.") from exc
    if not is_web_safe(url):
        raise OverlayImageError("That link isn't an image a browser can display. Upload the file instead.")
    try:
        return materialize_media_item(
            location=owner.location,
            profile=profile,
            source="external_url",
            url=url,
            page_url="",
            caption=caption,
            **({"pin": owner} if isinstance(owner, Pin) else {"wiki": owner}),
        )
    except MaterializeError as exc:
        logger.info("overlay external-url materialize failed: %s", exc)
        raise OverlayImageError("Couldn't download that image for an overlay.") from exc


def create_overlay(
    owner: Pin | Wiki,
    *,
    profile: Profile,
    corners: Sequence[Sequence[float]],
    image: Image | None = None,
    tile_url_template: str = "",
    name: str = "",
    opacity: int = 70,
    order: int | None = None,
    locked: bool = False,
    default_visible: bool = True,
    uuid: str | None = None,
) -> MapImageOverlay:
    """Create one overlay on *owner*'s map, enforcing the per-map cap.

    The owner row is locked for the count, so two concurrent creates cannot both see room for one more.

    Args:
        owner: The Pin or Wiki whose map gets the overlay.
        profile: The creator.
        corners: Four ``[lat, lng]`` pairs in ``CORNERS`` order.
        image: The stored image to draw; exactly one of this and *tile_url_template*.
        tile_url_template: An XYZ template for an already-warped sheet.
        name: Label for the layers panel (truncated to the column).
        opacity: Percent opacity.
        order: Draw order; None puts it on top of the owner's existing overlays.
        locked: Whether the corner handles start hidden.
        default_visible: Whether it starts toggled on.
        uuid: A uuid to keep (an import restoring its archived identity), when it is free.

    Returns:
        The saved overlay.

    Raises:
        OverlayLimitError: The map is already full.
        ValueError: Neither or both of *image* and *tile_url_template* were given, or *corners* is not four pairs.
    """
    from urbanlens.dashboard.services.core.text_limits import column_max_length

    if (image is None) == (not tile_url_template):
        raise ValueError("An overlay draws exactly one of an image or a tile template.")
    with transaction.atomic():
        type(owner).objects.select_for_update().filter(pk=owner.pk).first()
        existing = overlays_on(owner)
        if existing.count() >= MAX_OVERLAYS_PER_MAP:
            raise OverlayLimitError
        if order is None:
            order = (existing.order_by("-order").values_list("order", flat=True).first() or 0) + 1
        overlay = MapImageOverlay(
            name=name.strip()[: column_max_length(MapImageOverlay, "name")],
            image=image,
            tile_url_template=tile_url_template,
            opacity=max(0, min(100, opacity)),
            order=order,
            locked=locked,
            default_visible=default_visible,
            profile=profile,
            **owner_kwargs(owner),
        )
        overlay.set_corners([[float(lat), float(lng)] for lat, lng in corners])
        if uuid and not MapImageOverlay.objects.filter(uuid=uuid).exists():
            overlay.uuid = uuid
        overlay.save()
    return overlay
