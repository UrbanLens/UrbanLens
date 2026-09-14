"""Auto-apply helpers for protected status labels that a plugin (not a user) sets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.meta import KIND_STATUS

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki


def add_demolished_status(pin: Pin) -> None:
    """Add the profile's "Demolished" status label to the pin if not already present.

    Args:
        pin: Pin instance whose statuses should be updated.
    """
    from urbanlens.dashboard.models.labels.model import Label

    demolished_label = Label.objects.filter(profile=pin.profile, kind=KIND_STATUS, name="Demolished").first()
    if demolished_label and not pin.labels.filter(pk=demolished_label.pk).exists():
        pin.labels.add(demolished_label)
        pin.save(update_fields=["updated"])


def add_demolished_status_to_wiki(wiki: Wiki) -> None:
    """Add the canonical global "Demolished" status label to the wiki if not already present.

    Args:
        wiki: Wiki instance whose statuses should be updated."""
    from urbanlens.dashboard.models.labels.model import Label

    demolished_label = Label.objects.filter(profile=None, kind=KIND_STATUS, name="Demolished").first()
    if demolished_label and not wiki.labels.filter(pk=demolished_label.pk).exists():
        wiki.labels.add(demolished_label)
