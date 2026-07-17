"""Aggregates a profile's personal "journal" - visit notes, ratings, and comments.

Adding a future journal entry type is one new ``_x_entries`` function appended
to ``_JOURNAL_SOURCES`` below - nothing else needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from django.urls import reverse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One row in a profile's Journal feed - a visit note, a rating, or a comment.

    Attributes:
        kind: One of "visit", "review", "comment".
        occurred_at: When this entry was posted (tz-aware).
        icon: Material icon name for the entry's card.
        title: The pin/wiki/trip this entry is about.
        subtitle: Secondary display text (e.g. "Visit note", "Wiki comment").
        body: The entry's free text, untruncated (visit notes or comment text).
        url: Link to the relevant detail page (with an anchor where one exists).
        rating: Star rating 0-5, only set for "review" entries.
    """

    kind: str
    occurred_at: datetime
    icon: str
    title: str
    subtitle: str
    body: str
    url: str
    rating: int | None = None


def _visit_entries(profile: Profile) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each PinVisit the profile wrote notes for."""
    from urbanlens.dashboard.models.visits.model import PinVisit

    visits = PinVisit.objects.filter(pin__profile=profile).exclude(notes__isnull=True).exclude(notes="").select_related("pin").order_by("-visited_at")
    for visit in visits:
        pin = visit.pin
        yield JournalEntry(
            kind="visit",
            occurred_at=visit.visited_at,
            icon="edit_note",
            title=pin.effective_name,
            subtitle="Visit note",
            body=visit.notes or "",
            url=reverse("pin.details", kwargs={"pin_slug": pin.slug}) + "#visit-history-panel",
        )


def _review_entries(profile: Profile) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each pin the profile has rated."""
    from urbanlens.dashboard.models.reviews.model import Review

    reviews = Review.objects.filter(profile=profile).select_related("pin").order_by("-created")
    for review in reviews:
        pin = review.pin
        yield JournalEntry(
            kind="review",
            occurred_at=review.created,
            icon="star",
            title=pin.effective_name,
            # No subtitle - the star row itself already makes it obvious this
            # entry is a rating, and the label was redundant next to it.
            subtitle="",
            body="",
            url=reverse("pin.details", kwargs={"pin_slug": pin.slug}),
            rating=review.rating,
        )


def _comment_entries(profile: Profile) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each comment the profile has posted, on pins, wikis, or trips."""
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    pin_wiki_comments = Comment.objects.filter(profile=profile).select_related("pin", "wiki", "wiki__location").order_by("-created")
    trip_comments = TripComment.objects.filter(author=profile).select_related("trip").order_by("-created")

    for comment in chain(pin_wiki_comments, trip_comments):
        if getattr(comment, "pin_id", None):
            title = comment.pin.effective_name
            subtitle = "Comment"
            url = reverse("pin.details", kwargs={"pin_slug": comment.pin.slug}) + "#comments"
        elif getattr(comment, "wiki_id", None):
            title = comment.wiki.name
            subtitle = "Wiki comment"
            url = reverse("location.wiki", kwargs={"location_slug": comment.wiki.location.slug}) + "#comments"
        else:
            trip = comment.trip
            title = trip.name
            subtitle = "Trip comment"
            url = reverse("trips.detail", kwargs={"trip_slug": trip.slug}) + "#trip-comments"

        yield JournalEntry(
            kind="comment",
            occurred_at=comment.created,
            icon="forum",
            title=title,
            subtitle=subtitle,
            body=comment.text,
            url=url,
        )


_JOURNAL_SOURCES: tuple[Callable[[Profile], Iterator[JournalEntry]], ...] = (
    _visit_entries,
    _review_entries,
    _comment_entries,
)


def get_journal_entries(profile: Profile) -> list[JournalEntry]:
    """Merge every registered journal source for a profile, sorted newest-first.

    Args:
        profile: The profile whose journal to build.

    Returns:
        List of JournalEntry across all sources, newest first.
    """
    entries: list[JournalEntry] = []
    for source in _JOURNAL_SOURCES:
        entries.extend(source(profile))
    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return entries
