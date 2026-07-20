"""Auto-apply helpers for protected status labels that a plugin (not a user) sets.

Mirrors ``services.visits.add_visited_status`` for the "Demolished" status -
also a protected label (``is_protected=True``, seeded alongside "Visited" in
``models.labels.signals.create_default_tags``) that a plugin determines and
applies automatically rather than the user picking it manually.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki


def add_demolished_status(pin: Pin) -> None:
    """Add the profile's "Demolished" status label to the pin if not already present.

    Args:
        pin: Pin instance whose statuses should be updated.
    """
    from urbanlens.dashboard.models.labels.model import Label

    demolished_label = Label.objects.filter(profile=pin.profile, kind="status", name="Demolished").first()
    if demolished_label and not pin.labels.filter(pk=demolished_label.pk).exists():
        pin.labels.add(demolished_label)
        pin.save(update_fields=["updated"])


def add_demolished_status_to_wiki(wiki: Wiki) -> None:
    """Add the canonical global "Demolished" status label to the wiki if not already present.

    Unlike a Pin, a Wiki has no owning profile - its ``labels`` are shared
    taxonomy visible to every user who can see the location - so this uses the
    one global (``profile=None``) "Demolished" label seeded by migration
    ``0087_seed_global_demolished_label`` rather than any one user's private
    copy.

    Args:
        wiki: Wiki instance whose statuses should be updated.
    """
    from urbanlens.dashboard.models.labels.model import Label

    demolished_label = Label.objects.filter(profile=None, kind="status", name="Demolished").first()
    if demolished_label and not wiki.labels.filter(pk=demolished_label.pk).exists():
        wiki.labels.add(demolished_label)
