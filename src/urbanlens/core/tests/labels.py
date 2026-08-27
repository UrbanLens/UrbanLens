"""Test helper for creating labels that may already exist.

Every new ``Profile`` is seeded with ~46 default labels by
``models.labels.signals``. Since migration 0042, ``Label`` is unique on
``(lower(name), profile, kind)``, so a fixture doing
``Label.objects.create(profile=p, name="Hospital", kind=KIND_CATEGORY)`` now
raises `IntegrityError` - it was silently creating a *second* "Hospital", which
is the duplication that migration exists to prevent.

``ensure_label`` expresses what those fixtures actually want: a label with this
identity, whatever else already exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile


def ensure_label(*, profile: Profile | None, name: str, kind: str, **attrs: Any) -> Label:
    """Return the label with this (profile, name, kind), creating it if absent.

    Args:
        profile: Owner, or None for a global label.
        name: Label name. Matched case-insensitively, like the constraint.
        kind: Label kind.
        **attrs: Applied when creating, and updated onto an existing row so a
            fixture asking for a particular icon/description still gets it.

    Returns:
        The label.
    """
    from urbanlens.dashboard.models.labels.model import Label

    existing = Label.objects.filter(profile=profile, name__iexact=name, kind=kind).first()
    if existing is not None:
        if attrs:
            for field, value in attrs.items():
                setattr(existing, field, value)
            existing.save(update_fields=[*attrs, "updated"])
        return existing
    return Label.objects.create(profile=profile, name=name, kind=kind, **attrs)
