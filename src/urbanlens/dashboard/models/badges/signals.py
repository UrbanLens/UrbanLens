"""Signals for Badge - creates default user-specific badges when a Profile is created."""

from __future__ import annotations

# Default category names mirror the keys in services/ai/keywords.py so that
# auto-categorisation can match against badges the user actually owns.
DEFAULT_CATEGORIES: list[str] = [
    "Airport",
    "Amusement Park",
    "Asylum",
    "Bank",
    "Bridge",
    "Bunker",
    "Cars",
    "Castle",
    "Church",
    "Factory",
    "Fire Tower",
    "Firehouse",
    "Funeral Home",
    "Graveyard",
    "Hospital",
    "Hotel",
    "House",
    "Laboratory",
    "Library",
    "Lighthouse",
    "Mall",
    "Mansion",
    "Military Base",
    "Monument",
    "Police Station",
    "Power Plant",
    "Prison",
    "Resort",
    "Ruins",
    "School",
    "Stadium",
    "Theater",
    "Traincar",
    "Train Station",
    "Tunnel",
]


def create_default_tags(sender, instance, created: bool, **kwargs) -> None:
    """Create default personal status and category badges for every new profile.

    Status badges:
        "Visited" is protected - it cannot be deleted or renamed.
        "Active", "Abandoned", and "Demolished" are also protected.

    Category badges:
        One badge per entry in DEFAULT_CATEGORIES. Users may delete or rename
        any of these after creation.
    """
    if not created:
        return
    from urbanlens.dashboard.models.badges.model import KIND_CATEGORY, KIND_STATUS, Badge

    status_defaults = [
        {"name": "Visited", "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True},
        {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90},
        {"name": "Active", "icon": "🟢", "color": "#009688", "order": 80, "is_protected": True},
        {"name": "Abandoned", "icon": "🏚️", "color": "#FF9800", "order": 70, "is_protected": True},
        {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60, "is_protected": True},
    ]
    for d in status_defaults:
        Badge.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_STATUS,
            defaults={k: v for k, v in d.items() if k != "name"},
        )

    total = len(DEFAULT_CATEGORIES)
    for i, name in enumerate(DEFAULT_CATEGORIES):
        Badge.objects.get_or_create(
            profile=instance,
            name=name,
            kind=KIND_CATEGORY,
            defaults={"order": total - i},
        )
