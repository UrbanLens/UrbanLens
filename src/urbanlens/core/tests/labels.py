"""Test helper for creating labels that may already exist."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile


def ensure_label(*, profile: Profile | None, name: str, kind: str, **attrs: Any) -> Label:
    """Return the label with this (profile, name, kind), creating it if absent.

    Args:
        profile: Owner, or None for a global label.
        name: Label name.
        kind: Label kind. **attrs: Applied when creating, and updated onto an existing row so a fixture asking for a particular icon/description still...

    Returns:
        The label."""
    from urbanlens.dashboard.models.labels.model import Label

    existing = Label.objects.filter(profile=profile, name__iexact=name, kind=kind).first()
    if existing is not None:
        if attrs:
            for field, value in attrs.items():
                setattr(existing, field, value)
            existing.save(update_fields=[*attrs, "updated"])
        return existing
    return Label.objects.create(profile=profile, name=name, kind=kind, **attrs)
