"""Reversible wiki changes: child-pin moves and aliases."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

MODEL_LABEL = "wiki_mutation"


def _expired(message: str) -> NoReturn:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _wiki(wiki_id: int, profile: Profile) -> Wiki:
    """The stashed wiki, only while ``profile`` may still open it - the same gate the forward edit passed."""
    from urbanlens.dashboard.services.wiki.wiki_access import wiki_accessible_to

    wiki = Wiki.objects.filter(pk=wiki_id).select_related("location", "parent_wiki__location").first()
    # One answer for "gone" and "no longer yours to see", so a refused undo cannot probe for the wiki.
    if wiki is None or not wiki_accessible_to(wiki, profile):
        _expired("This wiki change can no longer be undone.")
    return wiki


def _move(wiki: Wiki, profile: Profile, latitude: float, longitude: float) -> None:
    before = [str(wiki.location.latitude), str(wiki.location.longitude)] if wiki.location_id else None
    location, _created = Location.objects.get_exact_or_create(latitude, longitude)
    wiki.location = location
    wiki.save(update_fields=["location", "updated"])
    # Recorded on the parent, like the forward move.
    WikiEdit.objects.create(
        wiki=wiki.parent_wiki or wiki,
        editor=profile,
        changes={"child_wiki_moved": {"pin": wiki.name, "from": before, "to": [str(location.latitude), str(location.longitude)]}},
    )


def _add_alias(wiki: Wiki, profile: Profile, payload: dict[str, Any]) -> None:
    alias = WikiAlias.objects.create(wiki=wiki, name=payload["name"], kind=payload.get("kind") or "alternate", created_by=profile)
    payload["alias_id"] = alias.pk
    WikiEdit.objects.create(wiki=wiki, editor=profile, changes={"alias_added": {"from": None, "to": payload["name"]}})


def _alias_removed(wiki: Wiki, profile: Profile, name: str) -> None:
    WikiEdit.objects.create(wiki=wiki, editor=profile, changes={"alias_removed": {"from": name, "to": None}})


def _rename(wiki: Wiki, profile: Profile, name: str) -> None:
    from urbanlens.dashboard.services.wiki.wiki_edits import apply_wiki_edit

    apply_wiki_edit(wiki, profile, {"name": name})


@register
class WikiMutationUndoHandler(MutationUndoHandler):
    """Undo/redo a wiki child-pin move or alias change, recorded in the wiki's history like the forward edit."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        op = payload.get("op")
        wiki = _wiki(payload["wiki_id"], profile)
        if op == "move":
            _move(wiki, profile, float(payload["before_lat"]), float(payload["before_lng"]))
            return
        if op == "alias_add":
            WikiAlias.objects.filter(pk=payload.get("alias_id"), wiki=wiki).delete()
            _alias_removed(wiki, profile, payload["name"])
            return
        if op == "alias_remove":
            _add_alias(wiki, profile, payload)
            WikiAutoRemoval.objects.filter(wiki=wiki, kind=AutoRemovalKind.ALIAS, value=payload["name"].casefold()).delete()
            return
        if op == "alias_promote":
            _rename(wiki, profile, payload["before_name"])
            return
        _expired(f"Unknown wiki mutation {op!r}.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        op = payload.get("op")
        wiki = _wiki(payload["wiki_id"], profile)
        if op == "move":
            _move(wiki, profile, float(payload["after_lat"]), float(payload["after_lng"]))
            return
        if op == "alias_add":
            _add_alias(wiki, profile, payload)
            return
        if op == "alias_remove":
            WikiAutoRemoval.objects.record(wiki=wiki, kind=AutoRemovalKind.ALIAS, value=payload["name"])
            WikiAlias.objects.filter(pk=payload.get("alias_id"), wiki=wiki).delete()
            WikiAlias.objects.filter(wiki=wiki, name__iexact=payload["name"]).delete()
            _alias_removed(wiki, profile, payload["name"])
            return
        if op == "alias_promote":
            _rename(wiki, profile, payload["after_name"])
            return
        _expired(f"Unknown wiki mutation {op!r}.")
