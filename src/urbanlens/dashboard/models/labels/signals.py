"""Signals for Label - default label creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.model import Label

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Default category definitions mirror the keys in services/labels/keyword_patterns.py so that
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
    """
    Create default personal status and category labels for every new profile.
    Status labels: "Visited" is protected - it cannot be deleted or renamed.
    """
    if not created:
        return
    from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_MEDIA, KIND_STATUS, KIND_TAG, KIND_USER
    from urbanlens.dashboard.plugins.points import LABELS_SEEDED

    status_defaults = [
        {"name": "Visited", "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True},
        # Protected like its four siblings: the "To Visit" saved filter every
        # new profile gets is built on this label, so deleting it would leave
        # that filter quietly matching the wrong thing.
        {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90, "is_protected": True},
        {"name": "Active", "icon": "🟢", "color": "#009688", "order": 80, "is_protected": True},
        {"name": "Abandoned", "icon": "🏚️", "color": "#FF9800", "order": 70, "is_protected": True},
        {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60, "is_protected": True},
    ]
    people_defaults = [
        {"name": "Preservation", "icon": "🌿", "color": "#4CAF50", "order": 40},
        {"name": "Vandalism", "icon": "⚠️", "color": "#F44336", "order": 30},
        {"name": "Photography", "icon": "📷", "color": "#2196F3", "order": 20},
        {"name": "Influencer", "icon": "📣", "color": "#9C27B0", "order": 10},
    ]
    total = len(DEFAULT_CATEGORIES)
    category_defaults = [{**cat, "order": total - i} for i, cat in enumerate(DEFAULT_CATEGORIES)]

    seeded = [
        Label(profile=instance, kind=kind, **fields)
        for kind, rows in (
            (KIND_STATUS, status_defaults),
            (KIND_CATEGORY, category_defaults),
            (KIND_TAG, DEFAULT_TAGS),
            (KIND_USER, people_defaults),
            (KIND_MEDIA, DEFAULT_MEDIA_LABELS),
        )
        for fields in rows
    ]
    # ignore_conflicts, so a name the profile already has in any case is kept rather than raising.
    Label.objects.bulk_create(seeded, ignore_conflicts=True)

    # Parent -> child links, so hierarchy-aware filtering (get_label_and_descendants) works for new profiles.
    category_by_name = {label.name.casefold(): label for label in Label.objects.for_profile(instance).categories()}
    links = [Label.parents.through(from_label=category_by_name[child.casefold()], to_label=category_by_name[parent.casefold()]) for parent, child in CATEGORY_HIERARCHY if parent.casefold() in category_by_name and child.casefold() in category_by_name]
    Label.parents.through.objects.bulk_create(links, ignore_conflicts=True)

    # bulk_create skips post_save, so whatever a plugin would have done on each label's save is announced here, once.
    LABELS_SEEDED.fire(instance)

    create_default_saved_filters(instance)


def create_default_saved_filters(profile: Profile) -> int:
    """
    Seed the two saved filters a new profile starts with.
    The main map's filter bar is otherwise an empty shelf that a user has to learn the label formula syntax to fill, and these two are what almost everyone builds first: what I have seen, and what I still want to see.

    Args:
        profile: The profile whose labels were just seeded.

    Returns:
        How many filters were created.
    """
    from urbanlens.dashboard.models.labels.meta import KIND_STATUS
    from urbanlens.dashboard.models.saved_filter.model import SavedFilter

    status_ids = dict(Label.objects.filter(profile=profile, kind=KIND_STATUS).values_list("name", "id"))
    visited, wanted, demolished = (status_ids.get("Visited"), status_ids.get("Want to Go"), status_ids.get("Demolished"))
    if visited is None or wanted is None:
        # Nothing to build the filters out of; the defaults above were
        # customised away or seeding is running against an unusual profile.
        return 0

    to_visit_groups: list[dict] = [
        # "or" carries the priority threshold so this reads as
        # "wanted *or* important", not "wanted *and* important".
        {"op": "or", "ids": [wanted], "min_priority": 4},
        {"op": "not", "ids": [visited, *([demolished] if demolished is not None else [])]},
    ]

    defaults = [
        ("Visited", "check_circle", "#4CAF50", 20, {"label_groups": [{"op": "and", "ids": [visited]}]}),
        ("To Visit", "flag", "#2196F3", 10, {"label_groups": to_visit_groups}),
    ]
    created = 0
    for name, icon, color, order, criteria in defaults:
        _filter, was_created = SavedFilter.objects.get_or_create(
            profile=profile,
            name=name,
            defaults={"icon": icon, "color": color, "order": order, "criteria": criteria},
        )
        created += int(was_created)
    return created
