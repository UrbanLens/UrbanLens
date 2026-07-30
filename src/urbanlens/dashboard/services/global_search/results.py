"""Result value objects shared by every global-search provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class ResultTypeMeta:
    """Display metadata for one result type (section header in the dialog).

    Attributes:
        slug: Stable identifier, also usable as a type filter in queries.
        label: Section heading shown to the user.
        icon: Material Symbols ligature for the section and its results.
    """

    slug: str
    label: str
    icon: str


#: Every result type, in the order sections are rendered in the dialog.
RESULT_TYPES: dict[str, ResultTypeMeta] = {
    meta.slug: meta
    for meta in (
        ResultTypeMeta("pins", "Pins", "push_pin"),
        ResultTypeMeta("photos", "Photos", "photo_library"),
        ResultTypeMeta("wikis", "Community wikis", "public"),
        ResultTypeMeta("articles", "Articles", "article"),
        ResultTypeMeta("trips", "Trips", "luggage"),
        ResultTypeMeta("visits", "Visits", "hiking"),
        ResultTypeMeta("messages", "Direct messages", "forum"),
        ResultTypeMeta("maps", "Markup maps", "draw"),
        ResultTypeMeta("safety", "Safety check-ins", "health_and_safety"),
        ResultTypeMeta("comments", "Comments", "chat_bubble"),
    )
}


@dataclass(slots=True)
class SearchResult:
    """One search hit, ready to render.

    ``url`` is a *web* path (``/map/pin/<slug>/``) built for the search dialog's
    anchor tags. A JSON client cannot follow it - there is no such route on the
    external API, and no amount of string surgery turns one into the other
    reliably (a wiki's web route is keyed by its location's slug, a photo has no
    web route of its own at all). ``object_slug``/``object_uuid`` exist so that
    surface never has to try: they carry the identifiers this codebase actually
    addresses the result by, and every provider is required to populate them.

    Attributes:
        type: A ``RESULT_TYPES`` slug.
        title: Primary line.
        url: Where clicking the result navigates *in the web UI*. Never send
            this to an API client - see the class docstring.
        subtitle: Secondary context line (place, participants, ...).
        snippet: Short excerpt showing why the item matched.
        icon: Material Symbols ligature; defaults to the type's icon.
        image_url: Optional thumbnail (photos, cover images).
        date: The item's most user-meaningful timestamp, for display.
        score: Relevance used to order results within a section.
        object_slug: The slug this result (or the resource that hosts it) is
            addressed by - a pin's slug for a pin, visit or pin comment, a
            location's slug for a wiki, wiki article or wiki comment, a trip's
            slug, a check-in's slug, the counterpart's profile slug for a direct
            message. ``""`` when the type is addressed by uuid alone (photos) or
            has no addressable host (a standalone markup map).
        object_uuid: The matched row's own uuid as a string, or None for the few
            models that carry none (``Article``, ``DirectMessage``,
            ``TripComment`` all extend the plain ``DashboardModel``). Present
            *alongside* ``object_slug`` rather than instead of it because a pin
            or trip whose slug has not been generated yet is still addressable by
            uuid - the API's ``<str:pin_slug>`` segment accepts either.
    """

    type: str
    title: str
    url: str
    subtitle: str = ""
    snippet: str = ""
    icon: str = ""
    image_url: str | None = None
    date: datetime | None = None
    score: float = 0.0
    object_slug: str = ""
    object_uuid: str | None = None

    def __post_init__(self) -> None:
        if not self.icon:
            meta = RESULT_TYPES.get(self.type)
            self.icon = meta.icon if meta else "search"


def excerpt(text: str | None, terms: list[str], *, radius: int = 45) -> str:
    """Return a short excerpt of ``text`` centered on the first matching term.

    Args:
        text: The haystack (may be None/empty).
        terms: Lowercased search terms; the first one found anchors the excerpt.
        radius: Characters of context kept on each side of the match.

    Returns:
        A trimmed excerpt with ellipses, or the leading slice of the text when
        no term matches, or "" for empty text.
    """
    if not text:
        return ""
    lowered = text.lower()
    index = -1
    for term in terms:
        index = lowered.find(term)
        if index >= 0:
            break
    if index < 0:
        return text[: radius * 2].strip() + ("..." if len(text) > radius * 2 else "")
    start = max(0, index - radius)
    end = min(len(text), index + radius)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet
