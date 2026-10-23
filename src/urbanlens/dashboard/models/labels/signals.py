"""Signals for Label - default label creation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.labels.model import Label

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

# Default category definitions mirror the keys in services/labels/keyword_patterns.py so that
# auto-categorisation can match against labels the user actually owns.
# Each entry carries an icon (emoji), a hex color, a description shown on the Organize page, and an optional display order.
DEFAULT_CATEGORIES: list[dict] = [
    {"name": "Amusement Park", "icon": "🎢", "color": "#FF9800", "description": "Theme parks, fairgrounds and water parks, with their rides and midways."},
    {"name": "Asylum", "icon": "🏚️", "color": "#673AB7", "description": "Psychiatric hospitals, state schools and sanatoriums."},
    {"name": "Bank", "icon": "🏦", "color": "#FFC107", "description": "Banks, vaults and old financial buildings."},
    {"name": "Bridge", "icon": "🌉", "color": "#607D8B", "description": "Road, rail and foot bridges, trestles and viaducts."},
    {"name": "Cave", "icon": "🪨", "color": "#795548", "description": "Natural caves, grottoes and mines."},
    {"name": "Castle", "icon": "🏰", "color": "#9C27B0", "description": "Castles, forts and fortified houses."},
    {"name": "Church", "icon": "⛪", "color": "#3F51B5", "description": "Churches, chapels, temples and other places of worship."},
    {"name": "Factory", "icon": "🏭", "color": "#607D8B", "description": "Mills, plants, foundries, warehouses and other industrial sites."},
    {"name": "Fire Tower", "icon": "🗼", "color": "#FF9800", "description": "Fire lookout towers and other watch towers."},
    {"name": "Graveyard", "icon": "🪦", "color": "#607D8B", "description": "Cemeteries, graveyards and mausoleums."},
    {"name": "Hospital", "icon": "🏥", "color": "#2196F3", "description": "Hospitals, clinics and medical centres."},
    {"name": "Hotel", "icon": "🏨", "color": "#FFC107", "description": "Hotels, motels, inns and lodges."},
    {"name": "House", "icon": "🏠", "color": "#8BC34A", "description": "Homes, farmhouses and other residences."},
    {"name": "Laboratory", "icon": "🔬", "color": "#00BCD4", "description": "Research labs, test facilities and observatories."},
    {"name": "Mall", "icon": "🏬", "color": "#E91E63", "description": "Shopping malls, department stores and retail plazas."},
    {"name": "Mansion", "icon": "🏡", "color": "#4CAF50", "description": "Estates, manors and other grand houses."},
    {"name": "Monument", "icon": "🗽", "color": "#9E9E9E", "description": "Memorials, statues and landmark structures."},
    {"name": "Morgue", "icon": "💀", "color": "#795548", "description": "Morgues, funeral homes and crematoriums."},
    {"name": "Park", "icon": "🏞️", "color": "#4CAF50", "description": "Parks, gardens and other outdoor public spaces."},
    {"name": "Parking", "icon": "🅿️", "color": "#607D8B", "description": "Places to park nearby, or parking structures worth visiting."},
    {"name": "Power Plant", "icon": "⚡", "color": "#FF5722", "description": "Power stations, substations, dams and other utilities."},
    {"name": "Prison", "icon": "🔒", "color": "#F44336", "description": "Prisons, jails, reformatories and detention centres."},
    {"name": "Resort", "icon": "🌴", "color": "#FFEB3B", "description": "Resorts, spas, camps and holiday complexes."},
    {"name": "Ruins", "icon": "🏛️", "color": "#795548", "description": "Remains of a structure, from foundations to standing shells."},
    {"name": "School", "icon": "🏫", "color": "#03A9F4", "description": "Schools, colleges and universities."},
    {"name": "Stadium", "icon": "🏟️", "color": "#4CAF50", "description": "Stadiums, arenas, racetracks and sports grounds."},
    {"name": "Theater", "icon": "🎭", "color": "#9C27B0", "description": "Theatres, cinemas, opera houses and music halls."},
    {"name": "Train Station", "icon": "🚉", "color": "#607D8B", "description": "Railway stations, depots, yards and roundhouses."},
    {"name": "Tunnel", "icon": "🕳️", "color": "#9E9E9E", "description": "Tunnels, drains, sewers and other underground passages."},
]
DEFAULT_TAGS: list[dict] = [
    {"name": "Notable", "icon": "⭐", "color": "#FFC107", "order": 50, "description": "Somewhere worth a special trip."},
    {"name": "Graffiti", "icon": "🎨", "color": "#E91E63", "order": 40, "description": "Known for its graffiti or street art."},
    {"name": "Photography", "icon": "📷", "color": "#2196F3", "order": 30, "description": "Good for photographs."},
    {"name": "Dangerous", "icon": "⚠️", "color": "#F44336", "order": 20, "description": "Has real hazards: unstable structure, hazardous materials, or other risks."},
    {"name": "Popular", "icon": "🔥", "color": "#FF5722", "order": 10, "description": "Busy and well known, so expect other visitors."},
]
# A small starter set of media labels (kind='media') - these help the user find
# their own photos/videos/documents via the main site search; they have no
# effect on pin icons or map filtering, unlike tag/category/status labels.
DEFAULT_MEDIA_LABELS: list[dict] = [
    {"name": "Interior", "icon": "🏠", "color": "#4CAF50", "order": 40, "description": "Taken inside."},
    {"name": "Exterior", "icon": "🌳", "color": "#8BC34A", "order": 30, "description": "Taken outside."},
    {"name": "Document", "icon": "📄", "color": "#607D8B", "order": 20, "description": "Scans of documents, plans, signs or papers."},
    {"name": "Video", "icon": "🎥", "color": "#9C27B0", "order": 10, "description": "Video footage."},
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
        {"name": "Visited", "icon": "✅", "color": "#4CAF50", "order": 100, "is_protected": True, "description": "Places you have been."},
        # Protected like its four siblings: the "To Visit" saved filter every
        # new profile gets is built on this label, so deleting it would leave
        # that filter quietly matching the wrong thing.
        {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90, "is_protected": True, "description": "Places you would like to visit."},
        {"name": "Active", "icon": "🟢", "color": "#009688", "order": 80, "is_protected": True, "description": "Still in use, occupied or open."},
        {"name": "Abandoned", "icon": "🏚️", "color": "#FF9800", "order": 70, "is_protected": True, "description": "Disused and left empty."},
        {"name": "Demolished", "icon": "💀", "color": "#795548", "order": 60, "is_protected": True, "description": "Torn down or otherwise gone."},
    ]
    people_defaults = [
        {"name": "Preservation", "icon": "🌿", "color": "#4CAF50", "order": 40, "description": "People working to preserve or document places."},
        {"name": "Vandalism", "icon": "⚠️", "color": "#F44336", "order": 30, "description": "People known to damage or deface places."},
        {"name": "Photography", "icon": "📷", "color": "#2196F3", "order": 20, "description": "Photographers."},
        {"name": "Influencer", "icon": "📣", "color": "#9C27B0", "order": 10, "description": "People with a public following who may draw crowds to a place."},
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
