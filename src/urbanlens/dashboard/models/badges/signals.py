"""Signals for Badge - creates default user-specific badges when a Profile is created."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Default category definitions mirror the keys in services/ai/keywords.py so that
# auto-categorisation can match against badges the user actually owns.
# Each entry carries an icon (emoji), a hex color, and an optional display order.
DEFAULT_CATEGORIES: list[dict] = [
    {"name": "Amusement Park", "icon": "🎢", "color": "#FF9800"},
    {"name": "Asylum", "icon": "🏚️", "color": "#673AB7"},
    {"name": "Bank", "icon": "🏦", "color": "#FFC107"},
    {"name": "Bridge", "icon": "🌉", "color": "#607D8B"},
    {"name": "Cave", "icon": "🪨", "color": "#795548"},
    {"name": "Castle", "icon": "🏰", "color": "#9C27B0"},
    {"name": "Church", "icon": "⛪", "color": "#3F51B5"},
    {"name": "Factory", "icon": "🏭", "color": "#607D8B"},
    {"name": "Fire Tower", "icon": "🗼", "color": "#FF9800"},
    {"name": "Graveyard", "icon": "🪦", "color": "#607D8B"},
    {"name": "Hospital", "icon": "🏥", "color": "#2196F3"},
    {"name": "Hotel", "icon": "🏨", "color": "#FFC107"},
    {"name": "House", "icon": "🏠", "color": "#8BC34A"},
    {"name": "Laboratory", "icon": "🔬", "color": "#00BCD4"},
    {"name": "Mall", "icon": "🏬", "color": "#E91E63"},
    {"name": "Mansion", "icon": "🏡", "color": "#4CAF50"},
    {"name": "Monument", "icon": "🗽", "color": "#9E9E9E"},
    {"name": "Morgue", "icon": "💀", "color": "#795548"},
    {"name": "Park", "icon": "🏞️", "color": "#4CAF50"},
    {"name": "Parking", "icon": "🅿️", "color": "#607D8B"},
    {"name": "Power Plant", "icon": "⚡", "color": "#FF5722"},
    {"name": "Prison", "icon": "🔒", "color": "#F44336"},
    {"name": "Resort", "icon": "🌴", "color": "#FFEB3B"},
    {"name": "Ruins", "icon": "🏛️", "color": "#795548"},
    {"name": "School", "icon": "🏫", "color": "#03A9F4"},
    {"name": "Stadium", "icon": "🏟️", "color": "#4CAF50"},
    {"name": "Theater", "icon": "🎭", "color": "#9C27B0"},
    {"name": "Train Station", "icon": "🚉", "color": "#607D8B"},
    {"name": "Tunnel", "icon": "🕳️", "color": "#9E9E9E"},
]

# Parent → child relationships established after all categories are created.
# Children are a specialisation or sub-type of the parent, which lets users
# filter a parent badge and surface pins tagged with any of its descendants.
CATEGORY_HIERARCHY: list[tuple[str, str]] = [
    # Healthcare
    ("Hospital", "Asylum"),       # psychiatric hospitals were often called asylums
    # Residential
    ("House", "Mansion"),         # a mansion is a grand house
    # Hospitality
    ("Hotel", "Resort"),          # resorts typically include hotel accommodation
    # Industrial
    ("Factory", "Power Plant"),   # power plants are large industrial facilities
    # Recreation
    ("Park", "Amusement Park"),   # an amusement park is a specialised park
]


def create_default_tags(sender: type[Profile], instance: Profile, created: bool, **kwargs) -> None:
    """Create default personal status and category badges for every new profile.

    Status badges:
        "Visited" is protected - it cannot be deleted or renamed.
        "Active", "Abandoned", and "Demolished" are also protected.

    Category badges:
        One badge per entry in DEFAULT_CATEGORIES, pre-populated with an icon,
        colour, and display order. Parent-child relationships from CATEGORY_HIERARCHY
        are wired up via the ``parents`` M2M after all badges exist. Users may delete
        or rename any category badge after creation.
    """
    if not created:
        return
    from urbanlens.dashboard.models.badges.model import KIND_CATEGORY, KIND_STATUS, KIND_USER, Badge

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
    for i, cat in enumerate(DEFAULT_CATEGORIES):
        Badge.objects.get_or_create(
            profile=instance,
            name=cat["name"],
            kind=KIND_CATEGORY,
            defaults={
                "icon": cat["icon"],
                "color": cat["color"],
                "order": total - i,
            },
        )

    # Wire up parent → child badge relationships so that hierarchy-aware
    # filtering (get_badge_and_descendants) works for new profiles.
    hierarchy_names = {name for pair in CATEGORY_HIERARCHY for name in pair}
    badge_by_name: dict[str, Badge] = {
        b.name: b
        for b in Badge.objects.filter(
            profile=instance,
            kind=KIND_CATEGORY,
            name__in=hierarchy_names,
        )
    }
    for parent_name, child_name in CATEGORY_HIERARCHY:
        parent = badge_by_name.get(parent_name)
        child = badge_by_name.get(child_name)
        if parent and child:
            child.parents.add(parent)

    people_defaults = [
        {"name": "Preservation", "icon": "🌿", "color": "#4CAF50", "order": 40},
        {"name": "Vandalism", "icon": "⚠️", "color": "#F44336", "order": 30},
        {"name": "Photography", "icon": "📷", "color": "#2196F3", "order": 20},
        {"name": "Influencer", "icon": "📣", "color": "#9C27B0", "order": 10},
    ]
    for d in people_defaults:
        Badge.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_USER,
            defaults={"icon": d["icon"], "color": d["color"], "order": d["order"]},
        )
