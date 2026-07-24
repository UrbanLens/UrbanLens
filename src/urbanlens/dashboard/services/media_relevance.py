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

from typing import TYPE_CHECKING

from django.db.models import Count

from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.spotguessr.model import GamePhotoFeedback, GamePhotoFeedbackKind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.location.model import Location

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

#: In-game thumbs down is deliberately given zero weight here. "Wrong photo
#: for this game" (blurry, ambiguous, gives away the answer too easily) is
#: not the same claim as "not relevant to this location's wiki" - folding it
#: into the wiki's own relevance signal would let bad *game* fit tank a
#: photo's standing on the wiki itself. The row is still recorded (see
#: GamePhotoFeedback) so this weighting choice stays visible/auditable and
#: reversible, it just isn't added into the score below.
_GAME_WEIGHTS = {
    GamePhotoFeedbackKind.THUMBS_UP: GAME_THUMBS_UP_WEIGHT,
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
        SpotGuessr gameplay signal (thumbs up/report/no-reaction; thumbs
        down is excluded - see ``_GAME_WEIGHTS``).
    """
    if not image.media_source_key or not image.media_item_key or image.location_id is None:
        return 0.0

    wiki_score = float(MediaRelevance.objects.vote_scores(image.location, image.media_source_key).get(image.media_item_key, 0))

    game_counts = GamePhotoFeedback.objects.filter(round__image=image).values("kind").annotate(n=Count("pk"))
    game_score = sum(_GAME_WEIGHTS.get(row["kind"], 0.0) * row["n"] for row in game_counts)

    return wiki_score + game_score


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
