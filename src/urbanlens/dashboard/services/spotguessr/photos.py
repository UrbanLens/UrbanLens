"""Photo selection for Photos-mode rounds.

See ``docs/designs/drafts/spotguessr.md`` ("Photo selection (Photos mode)") for why
there is deliberately no separate "opted into the game" gate yet - that
gate is UL-394's community submission pipeline.

**Privacy invariant, non-negotiable**: every candidate is gated on
``wiki__isnull=False``, *unless* it's ``solo_profile``'s own pin photo (see
``candidate_image_for_location``'s ``solo_profile`` arg) - the one narrow
exception, because the only person who could ever see that round is the pin's
own owner. A photo only reaches a wiki by being explicitly submitted there (or,
once UL-394 exists, by being uploaded specifically for the game, which will
attach it to the wiki at upload time) - it is the one signal that reliably
distinguishes "this profile chose to make this public" from "this is sitting
in someone's personal pin gallery, never shared." ``Image.wiki`` being null
must always be read as "not eligible", never as "eligible with a caveat" - a
private pin photo (or a safety check-in photo, a DM attachment, ...) leaking
into *another* profile's game session would be a serious privacy breach, not
a quality issue. Do not weaken or bypass this filter without an explicit,
deliberate sharing action (or the solo-viewer guarantee above) backing it.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.images.model import Image, ImageSource, MediaKind
from urbanlens.dashboard.services.media.media_relevance import effective_relevance

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


def candidate_image_for_location(
    location: Location,
    *,
    allow_arbitrary_external_photos: bool = False,
    solo_profile: Profile | None = None,
) -> Image | None:
    """Pick a photo to show for ``location``, or None if it has no eligible photo.

    Args:
        location: The round's answer location.
        allow_arbitrary_external_photos: When False (the default -
            ``config.allow_arbitrary_external_photos``), an externally-
            sourced candidate must have a *non-negative*
            ``services.media.media_relevance.effective_relevance`` score - a
            community-reported (or heavily downvoted) wiki photo is skipped
            rather than shown. Non-negative rather than strictly positive on
            purpose: almost every wiki photo starts at exactly 0 (no votes
            at all yet), and it's this very default mode that's expected to
            grow that score via "shown, no reaction" impressions - requiring
            a positive score before a photo can ever be shown would mean it
            could never accumulate one. When True, any wiki-attached
            externally-sourced photo is eligible regardless of score,
            including ones already known to be bad; this trades photo
            quality for pool size and (per the game's secondary goal of
            improving the site's own relevance data) still generates
            gameplay signal on exactly the photos that most need it.
            Personal uploads shared to the wiki are always eligible either
            way - they have no Media-gallery relevance identity to score in
            the first place.
        solo_profile: The session's one JOINED participant, passed only when
            this round is being generated for a single-participant session
            (``services.spotguessr.modes._build_photos`` passes ``None`` for
            any session with two or more joined participants). When set,
            ``solo_profile``'s own pin photos at ``location`` become eligible
            too, alongside wiki photos - see the module docstring's privacy
            invariant for why this is safe. Eligibility
            (``services.spotguessr.eligibility.eligible_locations``) already
            guarantees ``solo_profile`` has ``location`` pinned, so a matching
            ``pin__profile`` photo is always ``solo_profile``'s own.
    """
    photo_filter = Q(wiki__isnull=False)
    if solo_profile is not None:
        photo_filter |= Q(pin__profile=solo_profile)
    images = Image.objects.filter(photo_filter, location=location, media_type=MediaKind.PHOTO)
    candidates = list(images)
    if not allow_arbitrary_external_photos:
        candidates = [image for image in candidates if image.source == ImageSource.UPLOAD or effective_relevance(image) >= 0]
    if not candidates:
        return None
    return random.choice(candidates)  # noqa: S311 # nosec: B311 - game content selection, not security-sensitive
