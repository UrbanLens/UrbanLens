"""Aggregates a profile's personal "journal" - visit notes, ratings, and comments.

Adding a future journal entry type is one new ``_x_entries`` function keyed
into ``JOURNAL_SOURCES`` below, plus the matching scope entry in the external
API's ``MemoriesJournalView`` - which fails closed on a source it has no scope
mapping for, so a new domain cannot reach API callers unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from django.urls import reverse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from datetime import datetime

    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One row in a profile's Journal feed - a visit note, a rating, or a comment.

    Attributes:
        kind: One of "visit", "review", "comment", "article".
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
            url=reverse("pin.details", kwargs={"pin_slug": pin.slug}) + "#tab-visits",
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
    trip_comments = TripComment.objects.by_author(profile)

    for comment in chain(pin_wiki_comments, trip_comments):
        if getattr(comment, "pin_id", None):
            title = comment.pin.effective_name
            subtitle = "Comment"
            url = reverse("pin.details", kwargs={"pin_slug": comment.pin.slug}) + "#tab-comments"
        elif getattr(comment, "wiki_id", None):
            title = comment.wiki.name
            subtitle = "Wiki comment"
            url = reverse("location.wiki", kwargs={"location_slug": comment.wiki.location.slug}) + "#tab-comments"
        else:
            trip = comment.trip
            title = trip.name
            subtitle = "Trip comment"
            url = reverse("trips.detail", kwargs={"trip_slug": trip.slug}) + "#trip-comments-panel"

        yield JournalEntry(
            kind="comment",
            occurred_at=comment.created,
            icon="forum",
            title=title,
            subtitle=subtitle,
            body=comment.text,
            url=url,
        )


def _article_entries(profile: Profile) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each article edit (pin or wiki) the profile has made."""
    from urbanlens.dashboard.models.article.model import ArticleRevision

    revisions = ArticleRevision.objects.filter(editor=profile).select_related("article", "article__pin", "article__wiki", "article__wiki__location").order_by("-created")
    for revision in revisions:
        article = revision.article
        if article.pin_id:
            title = article.pin.effective_name
            subtitle = "Article edit"
            url = reverse("pin.details", kwargs={"pin_slug": article.pin.slug}) + "#tab-article"
        else:
            title = article.wiki.name
            subtitle = "Wiki article edit"
            url = reverse("location.wiki", kwargs={"location_slug": article.wiki.location.slug}) + "#tab-article"

        yield JournalEntry(
            kind="article",
            occurred_at=revision.created,
            icon="article",
            title=title,
            subtitle=subtitle,
            body=revision.edit_summary or revision.content,
            url=url,
        )


#: Journal sources by key, in the order they are declared.
#:
#: Keyed rather than a bare tuple because the journal is a *multi-domain*
#: aggregate: a visit note, a pin comment, a trip comment and a wiki article
#: body are four different privacy domains that happen to share one feed. The
#: internal Memories page always wants all four, but the external API must be
#: able to serve only the subset a credential's scopes cover, and it can only
#: do that if the sources are individually addressable. See
#: ``external_api.views.MemoriesJournalView.JOURNAL_SOURCE_SCOPES``, which maps
#: these keys onto scopes.
#:
#: Adding a source means adding an entry here *and* a scope entry there - the
#: view fails closed on an unmapped key, so a new domain cannot be exposed by
#: forgetting the second half.
JOURNAL_SOURCES: dict[str, Callable[[Profile], Iterator[JournalEntry]]] = {
    "visits": _visit_entries,
    "reviews": _review_entries,
    "comments": _comment_entries,
    "articles": _article_entries,
}


def get_journal_entries(profile: Profile, sources: Iterable[str] | None = None) -> list[JournalEntry]:
    """Merge journal sources for a profile, sorted newest-first.

    Args:
        profile: The profile whose journal to build.
        sources: Keys from :data:`JOURNAL_SOURCES` to include. None (the
            default) means every source, which is what the internal Memories
            page wants; the external API passes the subset its caller's scopes
            allow. Unknown keys are ignored rather than raising, so a caller
            filtering against a stale key list degrades to fewer entries rather
            than a 500.

    Returns:
        List of JournalEntry across the selected sources, newest first.
    """
    selected = JOURNAL_SOURCES.values() if sources is None else [JOURNAL_SOURCES[key] for key in sources if key in JOURNAL_SOURCES]
    entries: list[JournalEntry] = []
    for source in selected:
        entries.extend(source(profile))
    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return entries
