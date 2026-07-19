"""Resolve how a profile's identity should be displayed to a given viewer.

Content involving a person (a message, a trip activity, a comment) stays
fully visible even when their privacy settings don't permit the viewer to
see their profile - only their name/avatar/profile-link are masked in that
case, since the viewer has no standing access to know who they are. This is
the shared building block behind ``services.direct_messages.display_identity_for``
(1:1 DMs) and every place multiple people's identities render together in a
shared space they don't all fully know each other in (trip member lists,
group chat members/messages).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from urbanlens.dashboard.models.profile.model import Profile

#: Default placeholder for a masked identity outside of any specific tone
#: (DMs use "Former contact" instead - see display_identity_for).
DEFAULT_MASKED_PLACEHOLDER = "Member"


def resolve_visible_identity(viewer: Profile | None, subject: Profile, *, placeholder: str = DEFAULT_MASKED_PLACEHOLDER) -> dict[str, Any]:
    """Return how ``subject`` should be displayed to ``viewer`` right now.

    Args:
        viewer: The profile viewing the shared space, or None for an
            anonymous/system viewer (always masked, unless ``subject`` allows
            ``ANYONE``).
        subject: The profile whose identity is being displayed.
        placeholder: Display name used when masked - callers showing several
            simultaneously-masked people in one list should use
            ``resolve_visible_identities`` instead so each gets a distinct
            placeholder rather than all sharing this same string.

    Returns:
        Dict with ``display_name``, ``display_avatar_url`` (str or None),
        ``display_profile_url`` (str or None), and ``is_masked`` (bool).
    """
    if subject.can_view_profile(viewer):
        from django.urls import reverse

        return {
            "display_name": subject.username,
            "display_avatar_url": subject.avatar.url if subject.avatar else None,
            "display_profile_url": reverse("profile.view_user", kwargs={"profile_slug": subject.slug}) if subject.slug else None,
            "is_masked": False,
        }
    return {
        "display_name": placeholder,
        "display_avatar_url": None,
        "display_profile_url": None,
        "is_masked": True,
    }


def resolve_visible_identities(viewer: Profile | None, subjects: Sequence[Profile]) -> dict[int, dict[str, Any]]:
    """Resolve display identity for several people shown together in one list.

    Two people masked in the same list would otherwise both show the exact
    same generic placeholder and the same flat fallback-avatar color,
    reading as the same person twice. Masked entries are numbered ("Member
    1", "Member 2", ...) in list order, and every entry (masked or not)
    gets a distinct ``avatar_color_class`` via ``services.avatar_colors`` -
    the color is derived from the real profile server-side and never
    discloses who a masked person is, but keeps them visually distinct from
    one another the same way unmasked members already are.

    Each ``subject`` is also mutated in place (``display_name``,
    ``display_avatar_url``, ``display_profile_url``, ``is_masked``,
    ``avatar_color_class`` set directly on it), so templates that already
    hold a reference to the same object (e.g. ``membership.profile``) can
    use it directly without a dict lookup by a template-side variable key,
    which Django's template language doesn't support.

    Args:
        viewer: The profile viewing the shared space.
        subjects: The people whose identities are being displayed together.

    Returns:
        Mapping of ``subject.pk`` to the same shape ``resolve_visible_identity``
        returns, plus an ``avatar_color_class`` key on every entry - for
        callers (e.g. per-message sender resolution) that can't rely on
        object-identity mutation because their own objects were built from a
        separate query than ``subjects``.
    """
    from urbanlens.dashboard.services.avatar_colors import assign_avatar_colors

    subjects = list(subjects)
    results: dict[int, dict[str, Any]] = {}
    masked_ordinal = 0
    for subject in subjects:
        identity = resolve_visible_identity(viewer, subject)
        if identity["is_masked"]:
            masked_ordinal += 1
            identity["display_name"] = f"Member {masked_ordinal}"
        results[subject.pk] = identity

    assign_avatar_colors(subjects, identity=lambda p: p.slug or str(p.pk))
    for subject in subjects:
        identity = results[subject.pk]
        identity["avatar_color_class"] = getattr(subject, "avatar_color_class", "")
        for key, value in identity.items():
            setattr(subject, key, value)
    return results


def mask_profile_references(viewer: Profile | None, refs: Iterable[Profile]) -> None:
    """Resolve and apply masked identity across EVERY reference to the same profiles.

    The same real profile routinely shows up as more than one distinct
    Python object instance when it's reached via different query paths in
    the same render - a Trip's ``creator`` FK vs. a ``TripMembership.profile``
    FK for that same person on another trip, or a top-level Comment's
    ``profile`` vs. one of their own replies' ``profile``.
    ``resolve_visible_identities``'s in-place mutation only reaches whichever
    instance is actually passed to it, so every occurrence has to be visited
    directly - deduplicating first (by pk, for a stable/consistent masked
    ordinal and to avoid resolving the same profile twice) and then applying
    the result back to every instance in ``refs``, not just the deduplicated
    ones.

    Args:
        viewer: The profile viewing the shared space.
        refs: Every Profile reference about to be rendered together -
            duplicates (by real identity, not object identity) expected.
    """
    refs = list(refs)
    unique_by_pk: dict[int, Profile] = {}
    for subject in refs:
        unique_by_pk.setdefault(subject.pk, subject)

    resolved = resolve_visible_identities(viewer, list(unique_by_pk.values()))
    for subject in refs:
        identity = resolved.get(subject.pk)
        if identity is None:
            continue
        for key, value in identity.items():
            setattr(subject, key, value)
