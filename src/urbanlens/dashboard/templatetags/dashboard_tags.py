"""Custom template tags and filters for the dashboard app."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from django import template
from django.utils.html import format_html, format_html_join

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable
    import datetime

    from django.utils.safestring import SafeString

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile

register = template.Library()


@register.filter
def in_list(value: Any, collection: Collection[Any]) -> bool:
    """Return True if value is found in collection.

    Usage: {{ value|in_list:some_set }}
    """
    return value in collection


@register.filter
def label_map_url(label_id: int) -> str:
    """Return the main map URL, pre-filtered to pins carrying this one label.

    Used by the Organize > Labels page's "View on map" button - the main
    map's ``_restoreFiltersFromUrl()`` (map/index.html) reads the
    ``label_groups`` query param on load and applies it as a filter, using
    the same JSON shape the filter panel itself builds
    (``[{"op": "or", "ids": [...]}]``, see ``SearchForm.parse_label_groups``).

    Usage: {{ label.id|label_map_url }}
    """
    from django.urls import reverse
    from django.utils.http import urlencode

    groups = json.dumps([{"op": "or", "ids": [label_id]}])
    return f"{reverse('map.view')}?{urlencode({'label_groups': groups})}"


@register.filter
def reaction_summary(message: Any) -> list[dict[str, Any]]:
    """Group a DirectMessage's reactions by emoji for template rendering.

    Usage: {{ message|reaction_summary }}

    Relies on the caller having `prefetch_related("reactions__profile")` on
    the message queryset to avoid N+1 queries across a thread.
    """
    from urbanlens.dashboard.services.direct_messages import reaction_summary as _reaction_summary

    return _reaction_summary(message)


@register.filter
def tombstone_text(message: Any, viewer_id: int) -> str | None:
    """Return tombstone text for `message` as seen by `viewer_id`, or None if it renders normally.

    Usage: {{ message|tombstone_text:viewer_id }}
    """
    return message.tombstone_text_for(viewer_id)


@register.filter
def group_share_for(message: Any, viewer_id: int) -> Any:
    """Return the viewer's own GroupMessageShare on a group message, or None.

    Usage: {{ message|group_share_for:viewer_id }}

    Relies on the caller having prefetched ``shares`` on the message queryset
    to avoid N+1 queries across a thread.
    """
    return message.share_for(viewer_id)


@register.filter
def message_preview(message: Any, viewer_id: int) -> str:
    """Short preview text for a DirectMessage, honoring its tombstone state for `viewer_id`.

    Used for reply-quote boxes so a quoted message that's been deleted or has
    expired for the viewer shows the same placeholder as the original bubble
    would, rather than leaking its content through the quote.

    Usage: {{ message|message_preview:viewer_id }}
    """
    tombstone = message.tombstone_text_for(viewer_id)
    if tombstone:
        return tombstone
    if message.is_encrypted:
        # The server can't read the body; the client swaps in the real preview
        # after decrypting (see the quote-box's data-e2ee-* attributes).
        return "🔒 Message"
    if message.body:
        return message.body[:80]
    if message.images.exists():
        return "📷 Photo"
    if message.markup_map_id:
        return "🗺️ Map"
    if message.map_removed:
        return "Map removed"
    return "Message"


@register.filter
def read_receipt_visible_to(message: Any, viewer_id: int) -> bool:
    """True if `viewer_id` (as this message's sender) may see that it's been read.

    Usage: {{ message|read_receipt_visible_to:viewer_id }}

    Gated on the *recipient's* `read_receipt_visibility` setting toward the
    sender - the underlying `read_at` timestamp is always recorded (needed for
    disappearing-message timing) regardless of whether it's ever shown.
    """
    if message.sender_id != viewer_id or message.read_at is None:
        return False
    from urbanlens.dashboard.models.profile.model import Profile

    return Profile.visibility_permits(message.recipient.read_receipt_visibility, message.recipient, message.sender)


@register.filter
def first_pin_directions_url(map_data: Any) -> str | None:
    """Return a Google Maps walking-directions URL for the first "pin" item in a map snapshot.

    Usage: {{ map_data|first_pin_directions_url }}

    Args:
        map_data: A MarkupMap snapshot dict (see `MarkupMap.to_snapshot`).

    Returns:
        A `google.com/maps/dir` URL, or None if the snapshot has no pin item.
    """
    if not isinstance(map_data, dict):
        return None
    for shape in map_data.get("markup") or []:
        if isinstance(shape, dict) and shape.get("type") == "pin":
            latlngs = shape.get("latlngs") or []
            if latlngs and len(latlngs[0]) >= 2:
                lat, lng = latlngs[0][0], latlngs[0][1]
                return f"https://www.google.com/maps/dir/?api=1&destination={lat},{lng}&travelmode=walking"
    return None


@register.filter
def tag_total_pins(tag: Label) -> int:
    """Return direct pin count plus all direct children's pin counts.

    Uses annotated pin_count when available (set by LabelQuerySet.with_pin_counts()).
    Falls back to DB queries only when annotations are absent.
    """
    total = getattr(tag, "pin_count", None)
    if total is None:
        total = tag.pins.count()
    for child in tag.children.all():
        child_count = getattr(child, "pin_count", None)
        total += child_count if child_count is not None else child.pins.count()
    return total


@register.filter
def to_json(value: Any) -> str:
    """Serialize ``value`` to a compact JSON string for embedding in an attribute.

    Django auto-escapes the returned string, so quotes become ``&quot;`` and the
    blob remains a well-formed HTML attribute value. When JavaScript later reads
    the attribute (e.g. ``input.value``) the browser decodes the entities back to
    valid JSON, so ``JSON.parse`` round-trips cleanly.

    Usage: ``<input value="{{ obj.map_data|to_json }}">``

    Args:
        value: Any JSON-serializable value (typically a dict from a JSONField).

    Returns:
        A JSON-encoded string, or an empty string when ``value`` is falsy.
    """
    if not value:
        return ""
    return json.dumps(value, separators=(",", ":"))


@register.filter
def get_attr(obj: Any, attr: str) -> Any:
    """Return getattr(obj, attr), useful in loops over field names.

    Usage: {{ object|get_attr:field_name }}
    """
    return getattr(obj, attr, "")


@register.filter
def filter_criteria_summary(criteria: dict[str, Any] | None) -> str:
    """Return a short, human-readable summary of a SavedFilter's criteria dict.

    Usage: {{ saved_filter.criteria|filter_criteria_summary }}

    Walks the well-known criteria keys (the same ones SearchForm/filter_criteria
    produce) and joins whichever are present into a compact "·"-separated
    string for a Filters-tab card, e.g. "name contains 'diner' · 4★+ · 2 tags".
    Region keys are summarized separately by the card (see the region label),
    not included here.
    """
    if not criteria:
        return "No conditions set"
    parts: list[str] = []
    if name := criteria.get("name"):
        parts.append(f'name contains "{name}"')
    if min_rating := criteria.get("min_rating"):
        parts.append(f"{min_rating}★+")
    if max_rating := criteria.get("max_rating"):
        parts.append(f"{max_rating}★ or less")
    if criteria.get("label_groups"):
        parts.append(f"{len(criteria['label_groups'])} label rule(s)")
    else:
        if tags := criteria.get("tags"):
            parts.append(f"{len(tags)} tag(s) included")
        if exclude_tags := criteria.get("exclude_tags"):
            parts.append(f"{len(exclude_tags)} tag(s) excluded")
    if has_visits := criteria.get("has_visits"):
        parts.append("visited" if has_visits == "yes" else "not visited")
    if criteria.get("min_priority") is not None or criteria.get("max_priority") is not None:
        parts.append("priority range")
    if criteria.get("min_danger") or criteria.get("max_danger"):
        parts.append("danger range")
    if criteria.get("min_vulnerability") is not None or criteria.get("max_vulnerability") is not None:
        parts.append("vulnerability range")
    if criteria.get("visited_after") or criteria.get("visited_before"):
        parts.append("visit date range")
    if criteria.get("created_after") or criteria.get("created_before"):
        parts.append("created date range")
    if criteria.get("custom_fields"):
        parts.append(f"{len(criteria['custom_fields'])} custom field(s)")
    if criteria.get("overlapping_pins"):
        parts.append("overlapping pins only")
    return " · ".join(parts) if parts else "No conditions set"


@register.filter
def dict_get(mapping: dict[Any, Any] | None, key: Any) -> Any:
    """Return mapping.get(key), for looking up a dict value by a template variable key.

    Usage: {{ my_dict|dict_get:some_key }}
    """
    return (mapping or {}).get(key)


@register.filter
def distance(distance_km: Any, units: str = "km") -> str:
    """Format a kilometre distance in the viewer's preferred unit.

    Distances are stored/computed internally in kilometres; this renders them in
    the unit from ``distance_units`` (exposed globally by the
    ``add_distance_units`` context processor), converting to miles when asked.

    Usage: {{ route.distance_km|distance:distance_units }}

    Args:
        distance_km: A numeric distance in kilometres (``None``/invalid -> "").
        units: A ``DistanceUnit`` value ("km" or "mi"); defaults to kilometres.

    Returns:
        A formatted string like ``"12.3 km"`` or ``"7.6 mi"``, or "" if the
        input is not a number.
    """
    from urbanlens.dashboard.services.units import format_distance

    try:
        value = float(distance_km)
    except (TypeError, ValueError):
        return ""
    return format_distance(value, units or "km")


@register.filter
def human_timesince(value: datetime.datetime | datetime.date) -> str:
    """Return a human-friendly relative time string.

    Returns 'just now' for times less than 1 minute ago instead of '0 minutes ago'.

    Usage: {{ comment.created|human_timesince }}
    """
    from django.utils.timesince import timesince

    result = timesince(value)
    # timesince returns e.g. "0\xa0minutes" for < 1 min (non-breaking space between number and unit)
    if result.startswith("0"):
        return "just now"
    return f"{result} ago"


@register.filter
def is_material_icon(value: str | None) -> bool:
    """Return True if value is a Material Icons name (ASCII letters/underscores only).

    Returns False for emoji or other Unicode characters, which are rendered as-is.

    Usage: {% if tag.icon|is_material_icon %}
    """
    return bool(value and re.match(r"^[a-z_]+$", str(value)))


@register.filter
def is_icon_url(value: str | None) -> bool:
    """Return True if value is a URL pointing at an uploaded custom icon image.

    Mirrors the client-side classification in the map marker builder: a custom
    icon's ``effective_icon`` resolves to its file URL (absolute or media-relative),
    which must be rendered as an ``<img>`` rather than a Material Icons glyph or emoji.

    Usage: {% if pin.effective_icon|is_icon_url %}
    """
    return bool(value and re.match(r"^(https?://|/)", str(value)))


@register.filter
def icon_keywords(value: str | None) -> str:
    """Return space-separated search keywords for an emoji icon character.

    Looks up ``value`` in ``ICON_KEYWORDS`` and returns the associated string,
    or an empty string when no keywords are registered for this emoji.

    Usage: data-keywords="{{ tag.icon|icon_keywords }}"
    """
    from urbanlens.dashboard.models.labels.meta import ICON_KEYWORDS

    return ICON_KEYWORDS.get(str(value), "")


@register.filter
def contact_picker_options(connections: Iterable[Profile]) -> list[dict[str, Any]]:
    """Return friend connections as JSON-serializable dicts for the contact picker's autocomplete/avatar data.

    Usage: {{ connections|contact_picker_options|json_script:"safety-contact-friends-data" }}
    """
    return [
        {
            "id": friend.pk,
            "username": friend.username,
            "full_name": friend.full_name or "",
            "avatar_url": friend.avatar.url if friend.avatar else "",
        }
        for friend in connections
    ]


@register.simple_tag
def tooltip_attrs(
    text: str,
    pos: str = "top",
    *,
    wide: bool = False,
    float_tip: bool = False,
) -> SafeString:
    """Return HTML attributes for a ``data-tooltip`` trigger.

    Usage::

        <button type="button" {% tooltip_attrs "Save your changes." pos="below" %}>

    Args:
        text: Tooltip copy shown on hover, focus, or tap.
        pos: Placement - ``top``, ``below``, ``left``, or ``right``.
        wide: Use a wider bubble for longer explanatory text.
        float_tip: Render via the floating JS layer (escapes ``overflow:hidden``).

    Returns:
        Safe HTML attribute string for the host element.
    """
    parts: list[SafeString] = [format_html('data-tooltip="{}"', text)]
    if pos and pos != "top":
        parts.append(format_html('data-tooltip-pos="{}"', pos))
    if wide:
        parts.append(format_html("data-tooltip-wide"))
    if float_tip:
        parts.append(format_html('data-tooltip-float="true"'))
    return format_html_join(" ", "{}", ((part,) for part in parts))


@register.inclusion_tag("dashboard/partials/ui/_tooltip_help.html")
def tooltip_help(text: str, pos: str = "", wide: bool = False) -> dict[str, object]:
    """Render a standard info-icon tooltip trigger.

    Args:
        text: Tooltip copy.
        pos: Optional placement override.
        wide: Wider bubble for longer copy.

    Returns:
        Context dict for ``_tooltip_help.html``.
    """
    return {"text": text, "pos": pos, "wide": wide}


@register.inclusion_tag("dashboard/partials/ui/_tooltip_label.html")
def tooltip_label(
    label: str,
    help_text: str = "",
    pos: str = "",
    wide: bool = False,
) -> dict[str, object]:
    """Render a label with an optional inline help tooltip.

    Args:
        label: Visible label text.
        help_text: Optional tooltip copy; omitted when empty.
        pos: Optional tooltip placement.
        wide: Wider bubble for longer copy.

    Returns:
        Context dict for ``_tooltip_label.html``.
    """
    return {"label": label, "help": help_text, "pos": pos, "wide": wide}
