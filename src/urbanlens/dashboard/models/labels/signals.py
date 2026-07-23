"""Signals for Label - creates default user-specific labels when a Profile is created."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Default category definitions mirror the keys in services/ai/keywords.py so that
# auto-categorisation can match against labels the user actually owns.
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
DEFAULT_TAGS: list[dict] = [
    {"name": "Notable", "icon": "⭐", "color": "#FFC107", "order": 50},
    {"name": "Graffiti", "icon": "🎨", "color": "#E91E63", "order": 40},
    {"name": "Photography", "icon": "📷", "color": "#2196F3", "order": 30},
    {"name": "Dangerous", "icon": "⚠️", "color": "#F44336", "order": 20},
    {"name": "Popular", "icon": "🔥", "color": "#FF5722", "order": 10},
]
# A small starter set of media labels (kind='media') - these help the user find
# their own photos/videos/documents via the main site search; they have no
# effect on pin icons or map filtering, unlike tag/category/status labels.
DEFAULT_MEDIA_LABELS: list[dict] = [
    {"name": "Interior", "icon": "🏠", "color": "#4CAF50", "order": 40},
    {"name": "Exterior", "icon": "🌳", "color": "#8BC34A", "order": 30},
    {"name": "Document", "icon": "📄", "color": "#607D8B", "order": 20},
    {"name": "Video", "icon": "🎥", "color": "#9C27B0", "order": 10},
]

# Parent → child relationships established after all categories are created.
# Children are a specialisation or sub-type of the parent, which lets users
# filter a parent label and surface pins tagged with any of its descendants.
CATEGORY_HIERARCHY: list[tuple[str, str]] = [
    # Healthcare
    ("Hospital", "Asylum"),  # psychiatric hospitals were often called asylums
    # Residential
    ("House", "Mansion"),  # a mansion is a grand house
    # Hospitality
    ("Hotel", "Resort"),  # resorts typically include hotel accommodation
    # Industrial
    ("Factory", "Power Plant"),  # power plants are large industrial facilities
    # Recreation
    ("Park", "Amusement Park"),  # an amusement park is a specialised park
]


def create_default_tags(sender: type[Profile], instance: Profile, created: bool, **kwargs) -> None:
    """Create default personal status and category labels for every new profile.

    Status labels:
        "Visited" is protected - it cannot be deleted or renamed.
        "Active", "Abandoned", and "Demolished" are also protected.

    Category labels:
        One label per entry in DEFAULT_CATEGORIES, pre-populated with an icon,
        colour, and display order. Parent-child relationships from CATEGORY_HIERARCHY
        are wired up via the ``parents`` M2M after all labels exist. Users may delete
        or rename any category label after creation.

    Tag labels:
        A small starter set of ordinary, user-owned tag labels. Users may edit or
        delete these exactly like tags they create themselves.

    Media labels:
        A small starter set of media labels, applied to photos/videos/documents
        (not pins) to help the user find them via the main site search.
    """
    if not created:
        return
    from urbanlens.dashboard.models.labels.model import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER, Label

    status_defaults = [
        {"name": "Visited", "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True},
        {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90},
        {"name": "Active", "icon": "🟢", "color": "#009688", "order": 80, "is_protected": True},
        {"name": "Abandoned", "icon": "🏚️", "color": "#FF9800", "order": 70, "is_protected": True},
        {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60, "is_protected": True},
    ]
    for d in status_defaults:
        Label.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_STATUS,
            defaults={k: v for k, v in d.items() if k != "name"},
        )

    total = len(DEFAULT_CATEGORIES)
    for i, cat in enumerate(DEFAULT_CATEGORIES):
        Label.objects.get_or_create(
            profile=instance,
            name=cat["name"],
            kind=KIND_CATEGORY,
            defaults={
                "icon": cat["icon"],
                "color": cat["color"],
                "order": total - i,
            },
        )

    # Wire up parent → child label relationships so that hierarchy-aware
    # filtering (get_label_and_descendants) works for new profiles.
    hierarchy_names = {name for pair in CATEGORY_HIERARCHY for name in pair}
    label_by_name: dict[str, Label] = {
        b.name: b
        for b in Label.objects.filter(
            profile=instance,
            kind=KIND_CATEGORY,
            name__in=hierarchy_names,
        )
    }
    for parent_name, child_name in CATEGORY_HIERARCHY:
        parent = label_by_name.get(parent_name)
        child = label_by_name.get(child_name)
        if parent and child:
            child.parents.add(parent)

    for d in DEFAULT_TAGS:
        Label.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_TAG,
            defaults={"icon": d["icon"], "color": d["color"], "order": d["order"]},
        )

    people_defaults = [
        {"name": "Preservation", "icon": "🌿", "color": "#4CAF50", "order": 40},
        {"name": "Vandalism", "icon": "⚠️", "color": "#F44336", "order": 30},
        {"name": "Photography", "icon": "📷", "color": "#2196F3", "order": 20},
        {"name": "Influencer", "icon": "📣", "color": "#9C27B0", "order": 10},
    ]
    for d in people_defaults:
        Label.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_USER,
            defaults={"icon": d["icon"], "color": d["color"], "order": d["order"]},
        )

    for d in DEFAULT_MEDIA_LABELS:
        Label.objects.get_or_create(
            profile=instance,
            name=d["name"],
            kind=KIND_MEDIA,
            defaults={"icon": d["icon"], "color": d["color"], "order": d["order"]},
        )
