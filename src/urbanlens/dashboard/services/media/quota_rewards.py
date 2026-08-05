"""Deciding which stored files don't count against their uploader's storage quota.

Two cases, both about not charging one user for storage the whole community
benefits from (see :class:`~urbanlens.dashboard.models.images.model.QuotaExemption`):

- A locally cached copy of an external provider's photo. The user who upvoted
  it into the cache didn't author it, and the cache exists so the gallery
  doesn't break when the provider's URL rots.
- A user's own photo, shared to a wiki, that enough other people marked
  relevant. The bonus is the reward for contributing it.

The exemption is stored on the row rather than recomputed per quota check,
because the community reward is deliberately one-way: once earned it is never
revoked, so a user who is comfortably inside their quota can't be pushed over
it retroactively by other people changing their votes. Recomputing would also
mean hashing every photo's URL and counting votes on every upload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.models.images.model import QuotaExemption

if TYPE_CHECKING:
    from urbanlens.dashboard.models.images.model import Image

logger = logging.getLogger(__name__)

#: Panel key the wiki's own "Photos" tab votes under - a user's contributed
#: photos are voted on there, not through an external provider's panel.
#: See ``controllers.wiki_media.WikiMediaProviderView._photos``.
WIKI_PHOTOS_SOURCE = "photos"


def is_cached_external_media(image: Image) -> bool:
    """Whether this row is a locally cached copy of an external provider's item.

    Args:
        image: The row to classify.

    Returns:
        True when the row carries the gallery identity that
        ``services.media.media_materialize`` stamps onto everything it caches.
    """
    return bool(image.media_source_key and image.media_item_key)


def community_relevant_vote_count(image: Image) -> int:
    """How many *other* people have marked this contributed photo relevant.

    The uploader's own vote is excluded - upvoting your own photo shouldn't
    earn you storage.

    Args:
        image: A user-uploaded row shared to a wiki.

    Returns:
        The number of distinct other profiles who marked it relevant, or 0
        when the photo has no gallery identity to be voted on.
    """
    from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key

    if image.location_id is None or not image.image:
        return 0

    votes = MediaRelevance.objects.filter(
        location_id=image.location_id,
        source=WIKI_PHOTOS_SOURCE,
        item_key=media_item_key(image.image.url),
        is_relevant=True,
    )
    if image.profile_id is not None:
        votes = votes.exclude(profile_id=image.profile_id)
    return votes.count()


def refresh_community_quota_bonus(image: Image) -> bool:
    """Grant the community quota bonus if this photo has now earned it.

    Only applies to a user's own upload that they shared to a wiki. Cached
    external media is already exempt for a different reason, and a photo that
    was never contributed to a wiki has no community to reward it.

    Idempotent, and never revokes an exemption already granted.

    Args:
        image: The photo a relevance vote just landed on.

    Returns:
        True if this call granted the exemption, False if it didn't apply or
        was already granted.
    """
    from urbanlens.dashboard.models.images.model import Image as ImageModel
    from urbanlens.dashboard.models.site_settings.model import SiteSettings

    if image.quota_exempt_reason or image.wiki_id is None or image.profile_id is None or is_cached_external_media(image):
        return False

    threshold = SiteSettings.get_current().community_photo_quota_bonus_votes
    if threshold <= 0:
        return False
    if community_relevant_vote_count(image) < threshold:
        return False

    # queryset.update rather than save() - this runs from inside vote handling,
    # and a post_save side effect there is exactly what the codebase's signal
    # guidance warns against.
    ImageModel.objects.filter(pk=image.pk, quota_exempt_reason="").update(quota_exempt_reason=QuotaExemption.COMMUNITY_CONTRIBUTION)
    image.quota_exempt_reason = QuotaExemption.COMMUNITY_CONTRIBUTION
    logger.info("Granted community quota bonus for image %s (profile %s)", image.pk, image.profile_id)
    return True
