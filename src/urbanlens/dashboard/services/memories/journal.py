"""Aggregates a profile's personal "journal" - visit notes, ratings, and comments.

Adding a future journal entry type is one new ``_x_entries`` function keyed
into ``JOURNAL_SOURCES`` below, plus the matching scope entry in the external
API's ``MemoriesJournalView`` - which fails closed on a source it has no scope
mapping for, so a new domain cannot reach API callers unnoticed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain
import logging
from typing import TYPE_CHECKING, Any

from django.urls import reverse

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from datetime import datetime

    from urbanlens.dashboard.models.profile.model import Profile


logger = logging.getLogger(__name__)


def _newest(queryset: Any, ordering: str, limit: int | None) -> Any:
    """The newest ``limit`` rows of ``queryset``, or all of them when unlimited.

    Args:
        queryset: The source rows.
        ordering: The field to order by, newest first.
        limit: How many rows this source may contribute, or None for all.

    Returns:
        The ordered, optionally sliced queryset.
    """
    ordered = queryset.order_by(ordering)
    return ordered if limit is None else ordered[:limit]


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


def _visit_entries(profile: Profile, limit: int | None = None) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each PinVisit the profile wrote notes for.

    Selects the whole pin -> location -> wiki chain, as every source here does:
    each entry's title is ``Pin.effective_name``, which falls through to
    ``Location.display_name``, which reads the linked ``Wiki``. Selecting only
    the pin left two queries per rendered row.
    """
    from urbanlens.dashboard.models.visits.model import PinVisit

    visits = _newest(PinVisit.objects.filter(pin__profile=profile).exclude(notes__isnull=True).exclude(notes=""), "-visited_at", limit).select_related("pin__location__wiki")
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


def _review_entries(profile: Profile, limit: int | None = None) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each pin the profile has rated."""
    from urbanlens.dashboard.models.reviews.model import Review

    reviews = _newest(Review.objects.filter(profile=profile), "-created", limit).select_related("pin__location__wiki")
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


def _comment_entries(profile: Profile, limit: int | None = None) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each comment the profile has posted, on pins, wikis, or trips.

    Two querysets, each limited separately: this source can therefore yield up
    to twice ``limit``, which the merge above discards down to one page. Taking
    ``limit`` across the pair instead would be wrong - all of it could come from
    whichever half happened to sort first.
    """
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    pin_wiki_comments = _newest(Comment.objects.filter(profile=profile), "-created", limit).select_related("pin__location__wiki", "wiki__location")
    trip_comments = _newest(TripComment.objects.by_author(profile), "-created", limit)

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


def _article_entries(profile: Profile, limit: int | None = None) -> Iterator[JournalEntry]:
    """Yield a JournalEntry for each article edit (pin or wiki) the profile has made."""
    from urbanlens.dashboard.models.article.model import ArticleRevision

    revisions = _newest(ArticleRevision.objects.filter(editor=profile), "-created", limit).select_related("article__pin__location__wiki", "article__wiki__location")
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
JOURNAL_SOURCES: dict[str, Callable[[Profile, int | None], Iterator[JournalEntry]]] = {
    "visits": _visit_entries,
    "reviews": _review_entries,
    "comments": _comment_entries,
    "articles": _article_entries,
}


def get_journal_entries(profile: Profile, sources: Iterable[str] | None = None, *, limit: int | None = None) -> list[JournalEntry]:
    """Merge journal sources for a profile, sorted newest-first.

    Args:
        profile: The profile whose journal to build.
        sources: Keys from :data:`JOURNAL_SOURCES` to include. None (the
            default) means every source, which is what the internal Memories
            page wants; the external API passes the subset its caller's scopes
            allow. Unknown keys are ignored rather than raising, so a caller
            filtering against a stale key list degrades to fewer entries rather
            than a 500.
        limit: Return at most this many entries, and let each source fetch at
            most this many rows. Exact, not approximate: the newest ``N`` of a
            union can only contain entries that are in some source's own newest
            ``N``, so no entry that belongs in the result is left behind.

    Returns:
        List of JournalEntry across the selected sources, newest first.
    """
    selected = JOURNAL_SOURCES.items() if sources is None else [(key, JOURNAL_SOURCES[key]) for key in sources if key in JOURNAL_SOURCES]
    entries: list[JournalEntry] = []
    for key, source in selected:
        # Isolated per source, for the reason the docstring above already gives for
        # unknown keys: a journal missing one domain beats a 500. That reasoning only
        # covered a key that isn't registered; a registered source that *raises* took
        # the whole journal down with it. extend() consumes the generator incrementally,
        # so a source failing partway keeps what it already yielded.
        try:
            entries.extend(source(profile, limit))
        except Exception:
            logger.exception("Journal source %r failed; omitting it from the journal", key)
    entries.sort(key=lambda entry: entry.occurred_at, reverse=True)
    return entries if limit is None else entries[:limit]


def _visit_count(profile: Profile) -> int:
    """How many visit notes the profile has written."""
    from urbanlens.dashboard.models.visits.model import PinVisit

    return PinVisit.objects.filter(pin__profile=profile).exclude(notes__isnull=True).exclude(notes="").count()


def _review_count(profile: Profile) -> int:
    """How many pins the profile has rated."""
    from urbanlens.dashboard.models.reviews.model import Review

    return Review.objects.filter(profile=profile).count()


def _comment_count(profile: Profile) -> int:
    """How many comments the profile has posted, across pins, wikis and trips."""
    from urbanlens.dashboard.models.comments.model import Comment
    from urbanlens.dashboard.models.trips.model import TripComment

    return Comment.objects.filter(profile=profile).count() + TripComment.objects.by_author(profile).count()


def _article_count(profile: Profile) -> int:
    """How many article revisions the profile has written."""
    from urbanlens.dashboard.models.article.model import ArticleRevision

    return ArticleRevision.objects.filter(editor=profile).count()


#: How many entries each source holds, without building any of them.
#:
#: A parallel mapping rather than a second return value from the sources
#: themselves, because counting is one `.count()` per queryset while yielding is
#: a full fetch - the whole point of the count is not to pay for the rows. Its
#: keys must match :data:`JOURNAL_SOURCES`, which
#: ``test_memories_journal.py`` asserts, since a source counted but not rendered
#: (or the reverse) shows a page count that does not match its own list.
JOURNAL_SOURCE_COUNTS: dict[str, Callable[[Profile], int]] = {
    "visits": _visit_count,
    "reviews": _review_count,
    "comments": _comment_count,
    "articles": _article_count,
}


def _source_count(key: str, profile: Profile) -> int:
    """How many entries one source holds."""
    return JOURNAL_SOURCE_COUNTS[key](profile)


class JournalFeed(Sequence[JournalEntry]):
    """A profile's journal as a sliceable sequence, fetched one slice at a time.

    Both consumers - the Memories page and the external API's journal endpoint -
    want a page of a merged feed plus its total. A plain list gives them that by
    building every entry from four unbounded querysets first, which is what this
    replaces. ``Paginator`` and DRF's paginator both ask a sequence only for its
    length and one slice, so this satisfies them without ever materialising the
    rest.

    The total is counted rather than derived from the entries, so it stays exact
    while the rows stay bounded.

    Args:
        profile: The profile whose journal this is.
        sources: Keys from :data:`JOURNAL_SOURCES`, or None for all of them.
    """

    def __init__(self, profile: Profile, sources: Iterable[str] | None = None) -> None:
        self.profile = profile
        self.sources = None if sources is None else [key for key in sources if key in JOURNAL_SOURCES]

    def __len__(self) -> int:
        """How many entries the whole feed holds."""
        keys = JOURNAL_SOURCES if self.sources is None else self.sources
        total = 0
        for key in keys:
            try:
                total += _source_count(key, self.profile)
            except Exception:
                # Same isolation as get_journal_entries: a source that cannot be
                # counted is one this feed will not render either.
                logger.exception("Journal source %r could not be counted; omitting it from the total", key)
        return total

    def __getitem__(self, index: int | slice) -> Any:
        """One entry, or one slice of them, fetched no deeper than it needs."""
        if isinstance(index, slice):
            stop = index.stop
            if stop is None:
                return get_journal_entries(self.profile, self.sources)[index]
            return get_journal_entries(self.profile, self.sources, limit=stop)[index]
        return get_journal_entries(self.profile, self.sources, limit=index + 1)[index]
