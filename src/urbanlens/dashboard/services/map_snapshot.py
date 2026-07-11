"""Shared sanitization for Leaflet map snapshots (``map_data`` JSON blobs).

A map snapshot is a small JSON document capturing a map view plus any freehand
markup a user drew on it (lines, arrows, shapes, text). It is the wire format
between the shared map composer dialog and the server: the composer submits a
snapshot in a form field, and the server materializes it into a standalone
:class:`~urbanlens.dashboard.models.markup.model.MarkupMap` (viewport fields +
``PinMarkup`` item rows) that the host model (comment, visit, trip comment)
links to. Read-side rendering converts back to this format via
``MarkupMap.to_snapshot()`` for the shared client-side ``MarkupEngine`` (see
``partials/_markup_engine.html``).

Because the blob is user-submitted and rendered back into the DOM, every field
is validated and clamped here before it is trusted. Keeping this logic in one
place ensures the comment composer and the visit composer stay in lock-step.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.dateformat import format as format_date

from urbanlens.dashboard.models.markup.meta import normalize_layer_mode

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.markup.model import MarkupMap
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_ALLOWED_SHAPE_TYPES = {"line", "arrow", "circle", "rect", "polygon", "text"}


def _is_valid_lat(v: object) -> bool:
    """Return True if ``v`` is a number in the valid latitude range."""
    return isinstance(v, int | float) and -90 <= v <= 90


def _is_valid_lng(v: object) -> bool:
    """Return True if ``v`` is a number in the valid longitude range."""
    return isinstance(v, int | float) and -180 <= v <= 180


def _sanitize_markup_color(v: object, fallback: str = "#e74c3c") -> str:
    """Return ``v`` if it is a 6-digit hex colour, otherwise ``fallback``."""
    if isinstance(v, str) and _HEX_COLOR_RE.match(v):
        return v
    return fallback


def _sanitize_optional_color(v: object) -> str | None:
    """Return ``v`` if it is a hex colour or the string ``"none"``, else None."""
    if v == "none":
        return "none"
    if isinstance(v, str) and _HEX_COLOR_RE.match(v):
        return v
    return None


def _sanitize_number(v: object, lo: float, hi: float, default: float) -> float:
    """Clamp ``v`` to ``[lo, hi]`` if numeric, else return ``default``."""
    try:
        n = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _sanitize_latlngs(raw: object) -> list[list[float]]:
    """Return only the valid ``[lat, lng]`` pairs found in ``raw``."""
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, list | tuple) and len(item) >= 2:
            lat, lng = item[0], item[1]
            if _is_valid_lat(lat) and _is_valid_lng(lng):
                result.append([float(lat), float(lng)])
    return result


def _sanitize_markup_shapes(shapes: object) -> list[dict]:
    """Return cleaned shape dicts, dropping malformed or unknown-typed entries."""
    if not isinstance(shapes, list):
        return []
    clean: list[dict] = []
    for s in shapes:
        if not isinstance(s, dict):
            continue
        shape_type = s.get("type")
        if shape_type not in _ALLOWED_SHAPE_TYPES:
            continue
        latlngs = _sanitize_latlngs(s.get("latlngs"))
        if not latlngs:
            continue
        entry: dict = {
            "type": shape_type,
            "latlngs": latlngs,
            "color": _sanitize_markup_color(s.get("color")),
            "stroke_width": _sanitize_number(s.get("stroke_width"), 1, 50, 3),
            "fill_opacity": _sanitize_number(s.get("fill_opacity"), 0, 100, 87),
            "border_opacity": _sanitize_number(s.get("border_opacity"), 0, 100, 100),
        }
        bc = _sanitize_optional_color(s.get("border_color"))
        if bc is not None:
            entry["border_color"] = bc
        if shape_type == "text":
            label = s.get("label", "")
            entry["label"] = str(label)[:500] if isinstance(label, str) else ""
        clean.append(entry)
    return clean


def sanitize_map_data(data: object) -> dict | None:
    """Validate and clamp a decoded map snapshot dict.

    Args:
        data: The decoded JSON value (expected to be a dict).

    Returns:
        A sanitized snapshot dict, or None if ``data`` is not a usable snapshot
        (e.g. missing/invalid centre coordinates).
    """
    if not isinstance(data, dict):
        return None
    center_lat = data.get("center_lat")
    center_lng = data.get("center_lng")
    if not (_is_valid_lat(center_lat) and _is_valid_lng(center_lng)):
        return None
    return {
        "center_lat": float(center_lat),  # type: ignore[arg-type]
        "center_lng": float(center_lng),  # type: ignore[arg-type]
        "zoom": _sanitize_number(data.get("zoom"), 1, 22, 13),
        "layer_mode": normalize_layer_mode(data.get("layer_mode")),
        "show_borders": bool(data.get("show_borders")),
        "markup": _sanitize_markup_shapes(data.get("markup") or data.get("shapes")),
    }


def parse_map_data(request: HttpRequest, field: str = "map_data") -> dict | None:
    """Extract, decode, validate, and sanitize a map snapshot from a POST field.

    Args:
        request: The HTTP request whose POST body carries the JSON blob.
        field: Name of the POST field holding the JSON (defaults to ``map_data``).

    Returns:
        Sanitized snapshot dict if a valid blob was submitted, else None.
    """
    raw = request.POST.get(field, "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Ignoring malformed map_data in POST field %r", field)
        return None
    return sanitize_map_data(data)


def default_markup_map_title(context: Pin | Wiki | None = None) -> str:
    """Return the default MarkupMap title to use when none was explicitly given.

    Args:
        context: The Pin or Wiki this map was created from, if known.

    Returns:
        ``"<pin/wiki name> - <date>"`` when a Pin/Wiki context is given,
        otherwise just the creation date (e.g. ``"Jul 10, 2026"``).
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

    date_str = format_date(timezone.localdate(), "M j, Y")
    if isinstance(context, Pin):
        return f"{context.effective_name} - {date_str}"
    if isinstance(context, Wiki):
        return f"{context.name} - {date_str}"
    return date_str


def materialize_markup_map(
    profile: Profile,
    snapshot: dict | None,
    existing_map: MarkupMap | None = None,
) -> MarkupMap | None:
    """Create/update/delete a MarkupMap so it matches a submitted snapshot.

    The single write-path helper for hosts that attach maps through the
    snapshot composer (comments, trip comments, pin visits): pass the
    sanitized snapshot from ``parse_map_data`` plus whatever map the host
    already links to, then store the returned map on the host FK.

    Args:
        profile: Owner for a newly created map.
        snapshot: Sanitized snapshot dict, or None when no map was submitted.
        existing_map: The map currently linked by the host, if any.

    Returns:
        The MarkupMap the host should now link to, or None when the map was
        removed (a now-unreferenced existing map is deleted).
    """
    from urbanlens.dashboard.models.markup.model import MarkupMap

    if snapshot is None:
        if existing_map is not None:
            existing_map.delete()
        return None
    if existing_map is not None and existing_map.profile_id == profile.pk:
        markup_map = existing_map
    else:
        markup_map = MarkupMap.objects.create(profile=profile, title=default_markup_map_title())
    markup_map.replace_items_from_snapshot(snapshot)
    return markup_map
