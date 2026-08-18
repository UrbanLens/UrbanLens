"""Signals for Label - default label creation, and REData label-suggestion taxonomy sync."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save
from django.dispatch import receiver

from urbanlens.dashboard.models.labels.meta import KIND_CATEGORY, KIND_TAG
from urbanlens.dashboard.models.labels.model import Label

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

_SUGGESTABLE_KINDS = (KIND_TAG, KIND_CATEGORY)

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
        # Protected like its four siblings: the "To Visit" saved filter every
        # new profile gets is built on this label, so deleting it would leave
        # that filter quietly matching the wrong thing.
        {"name": "Want to Go", "icon": "⭐", "color": "#2196F3", "order": 90, "is_protected": True},
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

    create_default_saved_filters(instance)


def create_default_saved_filters(profile: Profile) -> int:
    """Seed the two saved filters a new profile starts with.

    The main map's filter bar is otherwise an empty shelf that a user has to
    learn the label formula syntax to fill, and these two are what almost
    everyone builds first: what I have seen, and what I still want to see.

    Built from the profile's own status labels, so they are ordinary saved
    filters - editable and deletable like any other. The labels they name are
    protected, so the filters cannot silently start matching the wrong thing.

    Args:
        profile: The profile whose labels were just seeded.

    Returns:
        How many filters were created.
    """
    from urbanlens.dashboard.models.labels.model import KIND_STATUS, Label
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
        {"op": "not", "ids": [visited, *( [demolished] if demolished is not None else [] )]},
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


# -- REData label-suggestion taxonomy sync --------------------------------
# Tag/category labels are created/edited/reparented/deleted from a couple
# dozen call sites (organize CRUD, bulk edit/convert/merge, the external API,
# default-label seeding above). Signals are the one choke point that sees
# all of them - see services.labels.redata_suggestions' module docstring.


@receiver(pre_save, sender=Label, dispatch_uid="label_remember_prior_kind_for_redata")
def remember_prior_kind_for_redata(sender: type[Label], instance: Label, **kwargs) -> None:
    """Stash the label's previously-saved kind so post_save can detect a kind change.

    Only a tag/category<->status transition (via LabelEditView/LabelBulkConvertView's
    kind conversion) needs this - see sync_redata_taxonomy_on_save.
    """
    instance.redata_prior_kind = Label.objects.filter(pk=instance.pk).values_list("kind", flat=True).first() if instance.pk else None


@receiver(post_save, sender=Label, dispatch_uid="label_sync_redata_taxonomy")
def sync_redata_taxonomy_on_save(sender: type[Label], instance: Label, created: bool, **kwargs) -> None:
    """Upsert a tag/category label's REData definition, or retire one that just left that kind."""
    from urbanlens.dashboard.services.labels.redata_suggestions import queue_label_definition_sync, queue_label_retirement

    prior_kind = None if created else getattr(instance, "redata_prior_kind", None)
    if instance.kind in _SUGGESTABLE_KINDS:
        queue_label_definition_sync(instance)
    elif prior_kind in _SUGGESTABLE_KINDS:
        queue_label_retirement(instance)


@receiver(post_delete, sender=Label, dispatch_uid="label_retire_redata_taxonomy_on_delete")
def retire_redata_taxonomy_on_delete(sender: type[Label], instance: Label, **kwargs) -> None:
    """Retire a deleted tag/category label's REData definition - REData has no hard delete."""
    from urbanlens.dashboard.services.labels.redata_suggestions import queue_label_retirement

    if instance.kind in _SUGGESTABLE_KINDS:
        queue_label_retirement(instance)


@receiver(m2m_changed, sender=Label.parents.through, dispatch_uid="label_parents_sync_redata_taxonomy")
def sync_redata_taxonomy_on_reparent(sender, instance: Label, action: str, reverse: bool, pk_set: set[int] | None, **kwargs) -> None:
    """Resync a label's REData definition after its parent set changes (parent_ids is part of it).

    ``Label.parents`` is symmetrical=False self-M2M, so both directions are
    real: ``child.parents.add(parent)`` (forward, instance is the child whose
    own parent_ids changed) and ``parent.children.add(child)`` /
    ``child.parents.add(parent)`` called from the parent side (reverse,
    instance is the parent, pk_set holds the affected children's ids - it is
    each child's definition that changed, not the parent's).
    """
    if action not in {"post_add", "post_remove", "post_clear"}:
        return
    from urbanlens.dashboard.services.labels.redata_suggestions import queue_label_definition_sync

    if reverse:
        children = Label.objects.filter(pk__in=pk_set or set(), kind__in=_SUGGESTABLE_KINDS)
    else:
        children = [instance] if instance.kind in _SUGGESTABLE_KINDS else []
    for child in children:
        queue_label_definition_sync(child)
