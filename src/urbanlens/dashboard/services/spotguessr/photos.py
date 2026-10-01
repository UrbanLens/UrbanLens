"""Photo selection for Photos-mode rounds. **Privacy invariant, non-negotiable**: every candidate is gated on ``wiki__isnull=False``, *unless* it's ``solo_profile``'s own pin photo (see ``candidate_image_for_location``'s ``solo_profile`` arg) - the..."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.services.media.media_relevance import effective_relevance

if TYPE_CHECKING:
    from collections.abc import Iterable

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


def _eligible_photo_filter(solo_profile: Profile | None) -> Q:
    """The module's non-negotiable privacy gate - see the module docstring.
    Shared by every eligibility check in this module so the wiki-only-unless- solo-viewer invariant can only ever be defined in one place."""
    photo_filter = Q(wiki__isnull=False)
    if solo_profile is not None:
        photo_filter |= Q(pin__profile=solo_profile)
    # A pending row is not servable to anyone (authorize_image refuses it, and an enrichment photo
    # has no profile to be its own exception), so a round built on one renders a broken image with
    # no fallback.
    # AND, not OR: this narrows every branch above, it is not another way in.
    return photo_filter & Q(pending_scan=False)


def candidate_image_for_location(
    location: Location,
    *,
    allow_arbitrary_external_photos: bool = False,
    solo_profile: Profile | None = None,
) -> Image | None:
    """Pick a photo to show for ``location``, or None if it has no eligible photo.

    Args:
        location: The round's answer location.
        allow_arbitrary_external_photos: When False (the default - ``config.allow_arbitrary_external_photos``), an externally- sourced candidate must have a *non-negative* ``services.media.media_relevance.effective_relevance`` score - a community-reported (or heavily downvoted) wiki...
        solo_profile: The session's one JOINED participant, passed only when this round is being generated for a single-participant session (``services.spotguessr.modes._build_photos`` passes ``None`` for any session with two or more joined participants)."""
    images = Image.objects.filter(_eligible_photo_filter(solo_profile), location=location, media_type=MediaKind.PHOTO)
    candidates = list(images)
    if not allow_arbitrary_external_photos:
        candidates = [image for image in candidates if image.source == ImageSource.UPLOAD or effective_relevance(image) >= 0]
    if not candidates:
        return None
    return random.choice(candidates)  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive


def locations_with_eligible_photo(location_ids: Iterable[int], *, solo_profile: Profile | None = None) -> list[int]:
    """Which of ``location_ids`` have at least one photo ``candidate_image_for_location`` could return.

    Args:
        location_ids: Candidate location ids (already eligibility-filtered).
        solo_profile: See ``candidate_image_for_location`` - the same narrow privacy exception applies here, using the identical filter.

    Returns:
        The subset of ``location_ids`` with at least one matching photo."""
    ids = list(location_ids)
    if not ids:
        return []

    return list(
        Image.objects.filter(_eligible_photo_filter(solo_profile), location_id__in=ids, media_type=MediaKind.PHOTO).values_list("location_id", flat=True).distinct(),
    )
