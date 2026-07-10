"""Undo handler for Wiki (community pages - in practice, only child/"detail" wikis are deletable today)."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import UndoHandler, register

_RESTORABLE_FIELDS = (
    "name",
    "description",
    "date_abandoned",
    "date_last_active",
    "pin_type",
    "color",
    "icon",
    "detail_bg_color",
    "detail_bg_opacity",
    "detail_border_color",
    "detail_border_opacity",
    "fences",
    "alarms",
    "cameras",
    "security",
    "signs",
    "vps",
    "plywood",
    "locked",
)


def with_wiki_descendants(wikis: list[Wiki]) -> list[Wiki]:
    """Expand ``wikis`` to include their full child-wiki subtree.

    Deleting a wiki cascades to its ``child_wikis`` (``Wiki.parent_wiki`` is
    ``on_delete=CASCADE``), so stashing only the given wikis would silently
    lose any nested child wikis on restore.

    Args:
        wikis: The wikis about to be deleted.

    Returns:
        ``wikis`` plus every descendant, as fresh Wiki instances.
    """
    all_ids = {w.pk for w in wikis}
    frontier = set(all_ids)
    while frontier:
        children = set(Wiki.objects.filter(parent_wiki_id__in=frontier).values_list("pk", flat=True))
        frontier = children - all_ids
        all_ids |= frontier
    return list(Wiki.objects.filter(pk__in=all_ids))


@register
class WikiUndoHandler(UndoHandler):
    """Restores a wiki's own fields, hierarchy position, and badges - not its cascade children.

    Comments, aliases, edit history, and photos are gone the instant the
    wiki is deleted and are not restored.
    """

    model_label = "wiki"

    @classmethod
    def serialize(cls, instances: list[Wiki]) -> list[dict[str, Any]]:
        return [cls._serialize_one(wiki) for wiki in instances]

    @classmethod
    def _serialize_one(cls, wiki: Wiki) -> dict[str, Any]:
        fields = {name: getattr(wiki, name) for name in _RESTORABLE_FIELDS}
        return {
            "old_pk": wiki.pk,
            "fields": fields,
            "location_id": wiki.location_id,
            "parent_wiki_old_pk": wiki.parent_wiki_id,
            "badge_ids": list(wiki.badges.values_list("id", flat=True)),
        }

    @classmethod
    def describe(cls, instances: list[Wiki]) -> str:
        if len(instances) == 1:
            return f"Wiki: {instances[0].name}"
        return f"{len(instances)} wiki pages"

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Wiki]:
        """Recreate wikis with fresh pks/uuids/slugs, relinking hierarchy and badges."""
        old_to_new: dict[int, Wiki] = {}
        restored: list[Wiki] = []
        for entry in payload:
            wiki = Wiki.objects.create(location_id=entry["location_id"], **entry["fields"])
            old_to_new[entry["old_pk"]] = wiki
            restored.append(wiki)

        for entry, wiki in zip(payload, restored, strict=True):
            old_parent_pk = entry["parent_wiki_old_pk"]
            if old_parent_pk and old_parent_pk in old_to_new:
                wiki.parent_wiki = old_to_new[old_parent_pk]
                wiki.save(update_fields=["parent_wiki"])
            if entry["badge_ids"]:
                wiki.badges.set(entry["badge_ids"])

        return restored
