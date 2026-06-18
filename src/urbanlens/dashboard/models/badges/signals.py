"""Signals for Badge - creates default user-specific badges when a Profile is created."""

from __future__ import annotations


def create_default_tags(sender, instance, created: bool, **kwargs) -> None:
    """Create default personal status-kind badges for every new profile.

    "Visited" is protected — it cannot be deleted or renamed.
    """
    if not created:
        return
    from urbanlens.dashboard.models.badges.model import KIND_STATUS, Badge

    defaults = [
        {"name": "Visited", "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True},
        {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90},
        {"name": "Active", "icon": "🟢", "color": "#009688", "order": 80},
        {"name": "Abandoned", "icon": "🏚️", "color": "#FF9800", "order": 70},
        {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60},
    ]
    for d in defaults:
        Badge.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_STATUS,
            defaults={k: v for k, v in d.items() if k != "name"},
        )
