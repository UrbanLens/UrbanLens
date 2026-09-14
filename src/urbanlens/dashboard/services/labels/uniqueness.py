"""One place that answers "would this label name collide?". - the check has to be **case-insensitive**, because the constraint is - refusing only exact matches would let "Abandoned" through and then fail at the database with an `IntegrityError`,..."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile


def find_conflicting_label(*, profile: Profile, name: str, kind: str, exclude_pk: int | None = None) -> Label | None:
    """Return the label *name* would collide with, or None.

    Args:
        profile: The owner the new/edited label belongs to.
        name: The proposed name.
        kind: The label kind the name must be unique within.
        exclude_pk: A label to ignore - the one being renamed, so a no-op rename of a label to its own name is not reported as a conflict with itself.

    Returns:
        The colliding label, preferring the user's own over a global one so the message can name something they can actually edit, or None when free."""
    from urbanlens.dashboard.models.labels.model import Label

    cleaned = (name or "").strip()
    if not cleaned:
        return None

    from django.db.models import F, Q

    candidates = Label.objects.filter(Q(profile=profile) | Q(profile__isnull=True), name__iexact=cleaned, kind=kind)
    if exclude_pk is not None:
        candidates = candidates.exclude(pk=exclude_pk)
    # Own labels first: "you already have a tag called X" is more actionable than naming a global
    # label the user cannot edit.
    # A global label has ``profile IS NULL``, and ``nulls_last`` puts those after real ids.
    return candidates.order_by(F("profile").asc(nulls_last=True)).first()


def label_conflict_message(conflict: Label, *, singular_title: str) -> str:
    """Phrase a collision for the user, distinguishing the two cases.

    Args:
        conflict: The label returned by :func:`find_conflicting_label`.
        singular_title: Human-readable kind, e.g. "Tag" or "Category".

    Returns:
        A message safe to show directly.
    """
    noun = singular_title.lower()
    if conflict.profile_id is None:
        return f'"{conflict.name}" is a built-in {noun} that everyone has, so you cannot create your own with that name. Use it directly, or pick a different name.'
    return f'You already have a {noun} called "{conflict.name}". Names are case-insensitive, so pick a different one - or merge the two from the Organize page.'
