"""Effective relevance for a materialized Media-gallery photo, and local-copy lookup.

Blends the wiki's own organic thumbs-up/down (``MediaRelevance`` - a
per-profile mark on a *transient* gallery item) with SpotGuessr gameplay
signal (``GamePhotoFeedback`` - an event log on a *materialized* ``Image``
row) into one score. The two are kept in entirely separate tables rather
than commingled into one running total, precisely so each signal can still
be queried/audited on its own - only ``effective_relevance()`` blends them,
and only for display/ranking purposes.
"""

from __future__ import annotations

from enum import StrEnum
import logging
from typing import TYPE_CHECKING, NamedTuple

from django.db.models import Count

from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile

#: In-game thumbs up counts toward the wiki's "relevant" signal at half the
#: weight of an explicit wiki vote - a deliberate positive reaction, but a
#: weaker one than a visitor specifically voting on this exact photo.
GAME_THUMBS_UP_WEIGHT = 0.5

#: In-game reports count toward "not relevant" at full weight - a report is
#: a direct, deliberate "this doesn't belong here" claim, the same claim an
#: explicit wiki downvote makes.
GAME_REPORT_WEIGHT = 1.0

#: Being shown with no reaction at all counts very weakly toward "relevant" -
#: most impressions are silent, so this only adds up to anything meaningful
#: across many plays.
GAME_NO_REACTION_WEIGHT = 0.01

#: In-game thumbs down counts toward "not relevant" at a token weight only -
#: "wrong photo for this game" (blurry, ambiguous, gives away the answer too
#: easily) is not the same claim as "not relevant to this location's wiki",
#: so it must never be enough on its own (or even in real numbers) to drag a
#: genuinely relevant photo's score to or below zero and knock it out of
#: eligibility. It still nudges the *ordering* among already-relevant photos
#: a little, which is the intended use - a future "sort by relevance"
#: ranking can use it to break ties among photos that are all positive.
GAME_THUMBS_DOWN_WEIGHT = 0.001

_GAME_WEIGHTS = {
    GamePhotoFeedbackKind.THUMBS_UP: GAME_THUMBS_UP_WEIGHT,
    GamePhotoFeedbackKind.THUMBS_DOWN: -GAME_THUMBS_DOWN_WEIGHT,
    GamePhotoFeedbackKind.REPORTED: -GAME_REPORT_WEIGHT,
    GamePhotoFeedbackKind.NO_REACTION: GAME_NO_REACTION_WEIGHT,
}


def effective_relevance(image: Image) -> float:
    """This photo's blended relevance score, or 0.0 if it has no relevance identity at all.

    Only meaningful for a materialized external-media row (``media_source_key``/
    ``media_item_key`` set - see ``Image.media_source_key``'s docstring). A
    plain personal upload was never voted on through the Media gallery and
    has no ``MediaRelevance``/``GamePhotoFeedback`` history to speak of;
    callers should treat those as trusted by default rather than reading the
    0.0 returned here as "not relevant".

    Args:
        image: A materialized ``Image`` row.

    Returns:
        wiki net vote score (each mark counts as +1/-1) plus the weighted
        SpotGuessr gameplay signal (thumbs up/down/report/no-reaction - see
        ``_GAME_WEIGHTS``).
    """
    if not image.media_source_key or not image.media_item_key or image.location_id is None:
        return 0.0

    wiki_score = float(MediaRelevance.objects.vote_scores(image.location, image.media_source_key).get(image.media_item_key, 0))

    game_counts = GamePhotoFeedback.objects.filter(round__image=image).values("kind").annotate(n=Count("pk"))
    game_score = sum(_GAME_WEIGHTS.get(row["kind"], 0.0) * row["n"] for row in game_counts)

    return wiki_score + game_score


def toggle_media_vote(image: Image, profile: Profile, *, value: int) -> int:
    """Cast, flip, or clear one profile's community vote on a materialized media row.

    The same three-state write ``controllers.wiki_media.WikiMediaVoteView``
    performs, addressed by an ``Image`` row instead of by a raw
    ``(source, item_key)`` pair from a gallery tile. Both end up in the same
    Location-scoped :class:`MediaRelevance` table, so a vote cast through the
    API and one cast on the wiki are the same vote - reusing this rather than
    reimplementing it is what keeps that true.

    Args:
        image: The materialized media row being voted on. Must carry the
            ``location``/``media_source_key``/``media_item_key`` identity that
            ``MediaRelevance`` is keyed by (see ``Image.media_source_key``).
        profile: The voting profile. One vote per profile per item; re-voting
            replaces the previous mark.
        value: ``1`` for relevant, ``-1`` for not relevant, ``0`` to withdraw
            an existing vote.

    Returns:
        The item's new net community score (up-votes minus down-votes across
        every contributing profile).

    Raises:
        ValueError: *value* isn't one of -1/0/1, or *image* has no relevance
            identity to vote on.
    """
    if value not in (-1, 0, 1):
        raise ValueError("Vote value must be -1, 0, or 1.")
    if image.location_id is None or not image.media_source_key or not image.media_item_key:
        raise ValueError("This photo has no gallery identity to vote on.")

    source = image.media_source_key
    item_key = image.media_item_key

    if value == 0:
        MediaRelevance.objects.for_gallery(profile, image.location, source).filter(item_key=item_key).delete()
    else:
        MediaRelevance.objects.update_or_create(
            profile=profile,
            location=image.location,
            source=source,
            item_key=item_key,
            defaults={"is_relevant": value == 1},
        )
        from urbanlens.dashboard.services.photos.redata_relevance import queue_relevance_vote

        queue_relevance_vote(image, profile, is_relevant=value == 1)

    return MediaRelevance.objects.vote_scores(image.location, source).get(item_key, 0)


#: Generic message shown when a download fails. ``MaterializeError`` can embed
#: raw requests/OSError text, which isn't safe to return verbatim, so every
#: caller surfaces this instead and logs the real reason server-side.
MATERIALIZE_ERROR_MESSAGE = "Could not save this photo."


class VotePolicy(StrEnum):
    """How a "relevant" mark should treat a vote the profile already cast.

    Attributes:
        EXPLICIT: The user deliberately clicked "relevant" - overwrite whatever
            they had before, including a previous down-vote.
        IMPLIED: Relevance is a side effect of some other action (adding the
            item to an album, say). A side effect must never silently reverse
            a deliberate down-vote, so those are left alone.
    """

    EXPLICIT = "explicit"
    IMPLIED = "implied"


class RelevantCacheResult(NamedTuple):
    """Outcome of :func:`record_relevant_and_cache`.

    Attributes:
        image: The materialized local copy, or None when nothing was cached
            (the vote was declined, materialization was skipped, the download
            failed, or it was deferred to a worker).
        voted: Whether a "relevant" vote was recorded by this call.
        declined: True when an :attr:`VotePolicy.IMPLIED` call found an
            existing down-vote and left it alone.
        error: A user-safe message when materialization failed, else None.
        queued: True when the download was handed to a Celery worker instead
            of run inline, so ``image`` is not populated yet.
    """

    image: Image | None
    voted: bool
    declined: bool
    error: str | None
    queued: bool = False


def record_relevant_and_cache(
    *,
    location: Location,
    profile: Profile,
    source: str,
    url: str,
    page_url: str = "",
    caption: str = "",
    pin=None,
    wiki=None,
    item_key: str = "",
    policy: VotePolicy = VotePolicy.EXPLICIT,
    materialize: bool = True,
) -> RelevantCacheResult:
    """Mark a transient gallery item relevant and cache a local copy of it.

    The one place the "write a MediaRelevance row, then download the item"
    sequence lives - shared by the pin and wiki vote endpoints and by the
    album add path, so the three can't drift.

    A failed download still leaves the vote in place: the user's opinion that
    this item matters is worth keeping even when today's fetch didn't work.

    Args:
        location: The location whose gallery the item belongs to.
        profile: The voting profile.
        source: Provider panel key (e.g. ``"wikimedia"``).
        url: The item's full-resolution image url.
        page_url: Optional provider page url, for attribution.
        caption: Optional caption carried from the gallery tile.
        pin: Cache the copy against this Pin (personal save), if given.
        wiki: Cache the copy against this Wiki (shared save), if given.
        item_key: The gallery tile's own item key. Defaults to hashing *url*,
            but a caller that already has one must pass it: a panel is free to
            key an item by something other than its image url, and writing the
            vote under a different key than the caller reads the score back
            with would silently lose it.
        policy: Whether this is a deliberate click or an implied side effect -
            see :class:`VotePolicy`.
        materialize: Set False to record the vote without downloading (the
            ``photos`` panel is already local, so there's nothing to fetch).

    Returns:
        A :class:`RelevantCacheResult`.
    """
    from urbanlens.dashboard.services.media.media_materialize import MaterializeError, materialize_media_item
    from urbanlens.dashboard.services.photos.redata_relevance import queue_relevance_vote

    item_key = item_key or media_item_key(url)
    if not item_key:
        return RelevantCacheResult(image=None, voted=False, declined=False, error="Missing item identity.")

    if policy is VotePolicy.IMPLIED:
        existing = MediaRelevance.objects.for_gallery(profile, location, source).filter(item_key=item_key).first()
        if existing is not None and not existing.is_relevant:
            return RelevantCacheResult(image=None, voted=False, declined=True, error=None)

    MediaRelevance.objects.update_or_create(
        profile=profile,
        location=location,
        source=source,
        item_key=item_key,
        defaults={"is_relevant": True},
    )

    if not materialize:
        return RelevantCacheResult(image=None, voted=True, declined=False, error=None)

    # Only pass the owner that's actually set - materialize_media_item scopes
    # its dedupe lookup on whichever of pin/wiki it receives, so handing it an
    # explicit None would be a different call than the owner-specific one.
    owner_kwargs = {}
    if pin is not None:
        owner_kwargs["pin"] = pin
    if wiki is not None:
        owner_kwargs["wiki"] = wiki

    try:
        image = materialize_media_item(
            location=location,
            profile=profile,
            source=source,
            url=url,
            page_url=page_url,
            caption=caption,
            **owner_kwargs,
        )
    except MaterializeError as exc:
        logger.warning("record_relevant_and_cache: failed to materialize %s: %s", url, exc)
        return RelevantCacheResult(image=None, voted=True, declined=False, error=MATERIALIZE_ERROR_MESSAGE)

    queue_relevance_vote(image, profile, is_relevant=True)
    return RelevantCacheResult(image=image, voted=True, declined=False, error=None)


def local_images_for_gallery_items(location: Location, source: str, urls: Iterable[str]) -> dict[str, Image]:
    """Bulk-lookup already-materialized local copies for a Media gallery panel's live results.

    Lets the pin-detail/wiki gallery prefer a cached local copy over hot-
    linking the provider whenever *any* profile has already materialized
    that exact item (e.g. by voting it relevant) - the remote url stays
    available too, callers should keep offering it as a "view original" link
    rather than dropping it once a local copy exists.

    Args:
        location: The location whose gallery is being rendered.
        source: The provider panel key (e.g. ``"wikimedia"``) - matched
            against ``Image.media_source_key``, not the translated
            ``ImageSource`` value.
        urls: Each item's full-resolution image url, as rendered by the
            panel (the same url ``media_item_key()`` is hashed from).

    Returns:
        Mapping of ``url`` to its materialized ``Image`` row, for every url
        that has one - urls with no local copy are simply absent.
    """
    from urbanlens.dashboard.models.images.model import Image

    keys_by_item_key = {media_item_key(url): url for url in urls}
    if not keys_by_item_key:
        return {}
    rows = Image.objects.filter(location=location, media_source_key=source, media_item_key__in=keys_by_item_key.keys())
    return {keys_by_item_key[row.media_item_key]: row for row in rows}
