"""Signals for Tag - creates default user-specific tags when a Profile is created."""

from __future__ import annotations


def create_default_tags(sender, instance, created: bool, **kwargs) -> None:
    """Create default personal tags for every new profile.

    These replace the former default PinLists ("Visited", "Want to Go").
    """
    if not created:
        return
    from urbanlens.dashboard.models.tags.model import Tag

    Tag.objects.get_or_create(
        profile=instance,
        name="Visited",
        defaults={"order": 1, "icon": "check_circle", "color": "#4CAF50"},
    )
    Tag.objects.get_or_create(
        profile=instance,
        name="Want to Go",
        defaults={"order": 0, "icon": "schedule", "color": "#2196F3"},
    )
