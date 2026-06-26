"""Mention parsing and rendering for comment text.

Storage formats:
  Location mention : @[Display Name](loc:{uuid})
  Activity mention : @act:{n}   (trip context only)

Rendering:
  - @loc mentions whose location UUID the viewer hasn't pinned → entire comment hidden
  - @loc mentions whose location UUID the viewer has pinned → rendered as hyperlink
  - @act:{n} → resolved via activity_index_map to an activity link
"""

from __future__ import annotations

import operator
import re
from typing import TYPE_CHECKING
import uuid

from django.utils.html import conditional_escape, format_html, format_html_join

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.utils.safestring import SafeString

    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.trips.model import TripActivity

_LOC_RE = re.compile(r"@\[([^\]]+)\]\(loc:([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\)")
_ACT_RE = re.compile(r"@act:(\d+)")


def extract_location_uuids(text: str) -> list[uuid.UUID]:
    """Return all location UUIDs referenced in comment text."""
    return [uuid.UUID(m.group(2)) for m in _LOC_RE.finditer(text)]


def is_visible_to(text: str, viewer_pinned_uuids: set[uuid.UUID]) -> bool:
    """Return False if any @loc mention in text is not in the viewer's pinned set."""
    return all(loc_uuid in viewer_pinned_uuids for loc_uuid in extract_location_uuids(text))


def render_comment_text(
    text: str,
    viewer_pinned_uuids: set[uuid.UUID],
    activity_index_map: dict[int, TripActivity] | None = None,
) -> str | None:
    """Render comment text to safe HTML, resolving @mentions.

    Returns None if the comment should be hidden from this viewer.
    ``activity_index_map`` maps map_index → TripActivity (trip context only).
    """
    if not is_visible_to(text, viewer_pinned_uuids):
        return None

    from django.urls import NoReverseMatch, reverse

    parts: list[str] = []
    last_end = 0

    # Collect and sort all mention spans
    mentions: list[tuple[int, int, SafeString]] = []

    for m in _LOC_RE.finditer(text):
        display = m.group(1)
        loc_uuid = m.group(2)
        try:
            wiki_url = reverse("location.wiki", args=[loc_uuid])
        except NoReverseMatch:
            wiki_url = "#"
        html = format_html(
            '<a href="{}" class="mention mention--location">@{}</a>',
            wiki_url,
            display,
        )
        mentions.append((m.start(), m.end(), html))

    if activity_index_map:
        for m in _ACT_RE.finditer(text):
            n = int(m.group(1))
            activity = activity_index_map.get(n)
            if activity is None:
                mentions.append((m.start(), m.end(), conditional_escape(m.group(0))))
                continue
            act_name = conditional_escape(activity.title or str(activity))
            if activity.location:
                try:
                    wiki_url = reverse("location.wiki", args=[str(activity.location.uuid)])
                    html = format_html(
                        '<a href="{}" class="mention mention--activity" data-activity-id="{}">@act:{} {}</a>',
                        wiki_url,
                        activity.id,
                        n,
                        act_name,
                    )
                except NoReverseMatch:
                    html = format_html("@act:{} {}", n, act_name)
            else:
                html = format_html(
                    '<span class="mention mention--activity" data-activity-id="{}">@act:{} {}</span>',
                    activity.id,
                    n,
                    act_name,
                )
            mentions.append((m.start(), m.end(), html))

    mentions.sort(key=operator.itemgetter(0))

    for start, end, html in mentions:
        parts.extend((conditional_escape(text[last_end:start]), html))
        last_end = end

    parts.append(conditional_escape(text[last_end:]))
    return format_html_join("", "{}", ((part,) for part in parts))


def viewer_pinned_uuids(profile: Profile) -> set[uuid.UUID]:
    """Return the set of Location UUIDs that profile has pinned."""
    from urbanlens.dashboard.models.pin.model import Pin

    raw = (
        Pin.objects.filter(profile=profile)
        .exclude(
            location__isnull=True,
        )
        .values_list("location__uuid", flat=True)
    )
    return {uuid.UUID(str(u)) for u in raw}


def filter_visible_comments(comments: Iterable[Comment], profile: Profile) -> list[Comment]:
    """Filter a queryset/list of Comment objects to those visible to profile."""
    pinned = viewer_pinned_uuids(profile)
    return [c for c in comments if is_visible_to(c.text, pinned)]
