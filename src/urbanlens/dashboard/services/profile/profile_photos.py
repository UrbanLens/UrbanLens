"""Photo-strip queries for the profile page - which of a profile's uploaded photos are safe to surface outside their original context."""

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
        Up to `STRIP_LIMIT` of the profile's own photos that are attached to a wiki or a direct message - never a bare/pin-only upload, which stays fully private."""
    return Image.objects.filter(profile=profile, media_type=MediaKind.PHOTO).filter(_not_fully_private()).select_related("wiki__location").order_by("-created")[:STRIP_LIMIT]


def strip_photos_visible_to(profile: Profile, viewer: Profile) -> QuerySet[Image]:
    """Photos from *profile*'s uploads that *viewer* already has an independent way to see.
    Deliberately conservative: only wiki-attached photos are included here.

    Args:
        profile: Whose uploads are being browsed.
        viewer: The profile viewing the page.

    Returns:
        Up to `STRIP_LIMIT` wiki-attached photos of profile's that viewer has pinned the location for (and whose upload/viewer photo- visibility settings otherwise permit - see `ImageQuerySet.visible_to`)."""
    from urbanlens.dashboard.services.wiki.wiki_access import visible_wiki_location_ids

    visible_location_ids = visible_wiki_location_ids(viewer)
    if not visible_location_ids:
        return Image.objects.none()
    return Image.objects.filter(profile=profile, media_type=MediaKind.PHOTO, wiki__location_id__in=visible_location_ids).visible_to(viewer).select_related("wiki__location").order_by("-created")[:STRIP_LIMIT]


def attachment_points_for_image(image: Image) -> list[dict]:
    """Describe where one of the owner's own photos is attached, for the lightbox side panel.

    Args:
        image: The image to describe.

    Returns:
        Dicts with `icon`/`label`/`url`, one per real attachment - empty if the photo isn't attached anywhere a second person could reach it."""
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
