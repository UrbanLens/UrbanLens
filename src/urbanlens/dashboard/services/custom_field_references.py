"""Access-scoped resolution of custom-field reference targets.

Reference-type custom fields (``CustomFieldType.REFERENCE``) point at one of the
user's own or visible objects: a pin, wiki, markup map, trip, uploaded photo,
pin list, or another user's profile. Everything here enforces the access rules
from the feature request - a user can only reference what they can already see:

- pins, photos, markup maps, and lists: only their own
- wikis: only wikis on locations they have pinned
- trips: only trips they are a member of
- profiles: only profiles whose identity they may view (picker offers friends)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.db.models import Q
from django.urls import NoReverseMatch, reverse

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The kinds of object a reference field may point at, in picker order.
REFERENCE_KINDS: list[tuple[str, str]] = [
    ("pin", "Pin"),
    ("wiki", "Wiki"),
    ("markup_map", "Map"),
    ("trip", "Trip"),
    ("photo", "Photo"),
    ("list", "List"),
    ("profile", "Person"),
]

#: Cap on picker/filter dropdown size, to keep rendered selects sane.
MAX_REFERENCE_CHOICES = 500


def referenceable_queryset(kind: str, profile: Profile) -> QuerySet:
    """The objects of ``kind`` the given profile is allowed to reference.

    Args:
        kind: A :data:`REFERENCE_KINDS` value.
        profile: The referencing user (the custom field's owner).

    Returns:
        An access-scoped queryset of candidate targets.

    Raises:
        ValueError: For an unknown kind.
    """
    from urbanlens.dashboard.models.friendship.model import Friendship
    from urbanlens.dashboard.models.images.model import Image
    from urbanlens.dashboard.models.markup.model import MarkupMap
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.pin_list.model import PinList
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel
    from urbanlens.dashboard.models.trips.model import Trip
    from urbanlens.dashboard.models.wiki.model import Wiki

    if kind == "pin":
        return Pin.objects.filter(profile=profile)
    if kind == "wiki":
        return Wiki.objects.filter(location__pins__profile=profile).select_related("location").distinct()
    if kind == "markup_map":
        return MarkupMap.objects.filter(profile=profile)
    if kind == "trip":
        return Trip.objects.filter(profiles=profile).distinct()
    if kind == "photo":
        return Image.objects.filter(profile=profile)
    if kind == "list":
        return PinList.objects.filter(profile=profile)
    if kind == "profile":
        friendships = Friendship.objects.filter(Q(from_profile=profile) | Q(to_profile=profile)).is_friend()
        friend_ids = {fs.to_profile_id if fs.from_profile_id == profile.pk else fs.from_profile_id for fs in friendships}
        return ProfileModel.objects.filter(pk__in=friend_ids)
    raise ValueError(f"Unknown reference kind {kind!r}.")


def resolve_reference(kind: str, pk: Any, profile: Profile) -> Any | None:
    """Resolve a candidate reference target by pk, enforcing access.

    Args:
        kind: A :data:`REFERENCE_KINDS` value.
        pk: The candidate target's primary key (any raw form; must parse as int).
        profile: The referencing user.

    Returns:
        The target instance, or None when it doesn't exist, isn't an int pk,
        or the profile may not reference it.
    """
    from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    if kind == "profile":
        # Resolution is deliberately broader than the (friends-only) picker:
        # any profile whose identity the user may view is a valid reference.
        candidate = ProfileModel.objects.filter(pk=pk).first()
        if candidate is not None and (candidate.pk == profile.pk or candidate.can_view_profile(profile)):
            return candidate
        return None
    try:
        return referenceable_queryset(kind, profile).filter(pk=pk).first()
    except ValueError:
        return None


def reference_label(kind: str, target: Any) -> str:
    """A short human-readable label for a reference target."""
    if target is None:
        return ""
    if kind == "pin":
        return target.effective_name or "Unnamed pin"
    if kind == "wiki":
        return target.name or "Unnamed wiki"
    if kind == "markup_map":
        return target.title or "Untitled map"
    if kind == "trip":
        return target.name or "Unnamed trip"
    if kind == "photo":
        return target.caption or Path(target.image.name).name or f"Photo {target.pk}"
    if kind == "list":
        return target.name or "Unnamed list"
    if kind == "profile":
        return target.username
    return str(target)


def reference_url(kind: str, target: Any) -> str | None:
    """The detail-page URL for a reference target, or None when it has none."""
    if target is None:
        return None
    try:
        if kind == "pin":
            return reverse("pin.details", args=[target.slug]) if target.slug else None
        if kind == "wiki":
            return reverse("location.wiki", args=[target.location.slug]) if target.location and target.location.slug else None
        if kind == "markup_map":
            return reverse("memories.maps")
        if kind == "trip":
            return reverse("trips.detail", args=[target.slug]) if target.slug else None
        if kind == "photo":
            return target.image.url if target.image else None
        if kind == "list":
            return reverse("lists.detail", args=[target.slug]) if target.slug else None
        if kind == "profile":
            return reverse("profile.view_user", args=[target.slug]) if target.slug else None
    except NoReverseMatch:
        logger.warning("Could not reverse a URL for custom-field reference kind %s (pk=%s)", kind, getattr(target, "pk", None))
    return None


def reference_choices(kind: str, profile: Profile, *, include_pk: int | None = None) -> list[tuple[int, str]]:
    """(pk, label) picker choices for a reference field, capped for sanity.

    Args:
        kind: A :data:`REFERENCE_KINDS` value.
        profile: The referencing user.
        include_pk: A pk to force into the list (the currently stored value)
            even when it falls outside the cap.

    Returns:
        Up to :data:`MAX_REFERENCE_CHOICES` (pk, label) tuples sorted by label,
        or an empty list for an unknown kind.
    """
    try:
        candidates = referenceable_queryset(kind, profile)[: MAX_REFERENCE_CHOICES + 1]
    except ValueError:
        return []
    choices = sorted(((target.pk, reference_label(kind, target)) for target in candidates), key=lambda pair: pair[1].lower())
    if len(choices) > MAX_REFERENCE_CHOICES:
        logger.info("Reference picker for kind %s capped at %s choices for profile %s", kind, MAX_REFERENCE_CHOICES, profile.pk)
        choices = choices[:MAX_REFERENCE_CHOICES]
    if include_pk is not None and all(pk != include_pk for pk, _ in choices):
        current = resolve_reference(kind, include_pk, profile)
        if current is not None:
            choices.append((current.pk, reference_label(kind, current)))
    return choices
