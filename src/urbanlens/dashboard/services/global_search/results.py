"""Result value objects shared by every global-search provider."""

from __future__ import annotations

from dataclasses import dataclass, field
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

    Attributes:
        type: A ``RESULT_TYPES`` slug.
        title: Primary line.
        url: Where clicking the result navigates.
        subtitle: Secondary context line (place, participants, ...).
        snippet: Short excerpt showing why the item matched.
        icon: Material Symbols ligature; defaults to the type's icon.
        image_url: Optional thumbnail (photos, cover images).
        date: The item's most user-meaningful timestamp, for display.
        score: Relevance used to order results within a section.
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
