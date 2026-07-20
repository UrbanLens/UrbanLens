"""Photo-strip queries for the profile page - which of a profile's uploaded
photos are safe to surface outside their original context.

A photo attached only to a private pin (the overwhelming default - pins are
never visible to anyone but their owner; PinShare copies a photo onto a
brand new Image row for the recipient rather than granting access to the
original, see controllers.pin_sharing._create_pin_from_share) is never
eligible for the strip, even on the owner's own profile - "not fully
private" specifically means the photo is *already* reachable through some
other path a second person could use: a wiki (anyone who's pinned that
location), or a direct message (the recipient, once actually granted
permission to see it - see strip_photos_visible_to's docstring for why that
second case is deliberately NOT extended to a second viewer here).

Trip-attached photos are not included: unlike wiki/pin/DM, Image has no
`trip` FK at all - there is no first-class "this photo is on a trip"
attachment in the current data model to check (TripComment.image is a
one-off plain ImageField, invisible to this gallery entirely). Treated as
an accurate scope limit, not a bug to silently paper over.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models import Q

from urbanlens.dashboard.models.images.model import Image, MediaKind

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

#: How many photos the strip shows at most.
STRIP_LIMIT = 24


def _not_fully_private() -> Q:
    """Photos attached to something a second person could reach independently."""
    return Q(wiki__isnull=False) | Q(direct_message__isnull=False)


def strip_photos_for_owner(profile: Profile) -> QuerySet[Image]:
    """Photos to show *profile* on their own profile page's strip.

    Args:
        profile: The profile whose own page is being viewed.

    Returns:
        Up to `STRIP_LIMIT` of the profile's own photos that are attached to
        a wiki or a direct message - never a bare/pin-only upload, which
        stays fully private.
    """
    return (
        Image.objects.filter(profile=profile, media_type=MediaKind.PHOTO)
        .filter(_not_fully_private())
        .select_related("wiki__location")
        .order_by("-created")[:STRIP_LIMIT]
    )


def strip_photos_visible_to(profile: Profile, viewer: Profile) -> QuerySet[Image]:
    """Photos from *profile*'s uploads that *viewer* already has an independent way to see.

    Deliberately conservative: only wiki-attached photos are included here.
    A direct-message attachment is a real "not fully private" signal for the
    owner's own strip (`strip_photos_for_owner`), but showing it to the DM
    partner here too would need to re-derive the same blur/consent
    (`DirectMessageImagePermission`/`images_revealed`) and soft-delete rules
    the message thread itself enforces - not attempted in this pass, so a
    DM-attached photo simply never appears on someone else's view of this
    strip. That's the safe failure mode (never shown, never leaked), not an
    oversight.

    Args:
        profile: Whose uploads are being browsed.
        viewer: The profile viewing the page.

    Returns:
        Up to `STRIP_LIMIT` wiki-attached photos of profile's that viewer
        has pinned the location for (and whose upload/viewer photo-
        visibility settings otherwise permit - see `ImageQuerySet.visible_to`).
    """
    from urbanlens.dashboard.services.wiki_access import visible_wiki_location_ids

    visible_location_ids = visible_wiki_location_ids(viewer)
    if not visible_location_ids:
        return Image.objects.none()
    return (
        Image.objects.filter(profile=profile, media_type=MediaKind.PHOTO, wiki__location_id__in=visible_location_ids)
        .visible_to(viewer)
        .select_related("wiki__location")
        .order_by("-created")[:STRIP_LIMIT]
    )


def attachment_points_for_image(image: Image) -> list[dict]:
    """Describe where one of the owner's own photos is attached, for the lightbox side panel.

    Args:
        image: The image to describe. Caller must have already confirmed
            this is the requesting profile's own photo (see
            `PhotoAttachmentPointsView`) - this function does no visibility
            checking of its own.

    Returns:
        Dicts with `icon`/`label`/`url`, one per real attachment - empty if
        the photo isn't attached anywhere a second person could reach it.
    """
    from django.urls import reverse

    points: list[dict] = []
    wiki = image.wiki
    if wiki is not None and wiki.location_id:
        points.append(
            {
                "icon": "public",
                "label": f"Wiki: {wiki.name or 'Untitled wiki'}",
                "url": reverse("location.wiki", args=[wiki.location.slug]),
            },
        )
    dm = image.direct_message
    if dm is not None:
        other = dm.recipient if dm.sender_id == image.profile_id else dm.sender
        if other is not None:
            points.append(
                {
                    "icon": "chat",
                    "label": f"Sent to {other.username}",
                    "url": reverse("messages.conversation", args=[other.slug]),
                },
            )
    return points
