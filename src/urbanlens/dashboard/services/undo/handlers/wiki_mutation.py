"""Reversible wiki changes: child-wiki moves and aliases."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.aliases.model import WikiAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

MODEL_LABEL = "wiki_mutation"


def _expired(message: str) -> None:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _wiki(wiki_id: int) -> Wiki:
    wiki = Wiki.objects.filter(pk=wiki_id).select_related("location").first()
    if wiki is None:
        _expired("This wiki no longer exists.")
    return wiki  # type: ignore[return-value]


def _move(wiki: Wiki, latitude: float, longitude: float) -> None:
    location, _created = Location.objects.get_exact_or_create(latitude, longitude)
    wiki.location = location
    wiki.save(update_fields=["location", "updated"])


@register
class WikiMutationUndoHandler(MutationUndoHandler):
    """Undo/redo a wiki child-pin move or alias change."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        wiki = _wiki(payload["wiki_id"])
        if op == "move":
            _move(wiki, float(payload["before_lat"]), float(payload["before_lng"]))
            return
        if op == "alias_add":
            WikiAlias.objects.filter(pk=payload.get("alias_id"), wiki=wiki).delete()
            return
        if op == "alias_remove":
            alias = WikiAlias.objects.create(
                wiki=wiki,
                name=payload["name"],
                kind=payload.get("kind") or "alternate",
            )
            payload["alias_id"] = alias.pk
            WikiAutoRemoval.objects.filter(wiki=wiki, kind=AutoRemovalKind.ALIAS, value=payload["name"].casefold()).delete()
            return
        if op == "alias_promote":
            wiki.name = payload["before_name"]
            wiki.save(update_fields=["name", "updated"])
            return
        _expired(f"Unknown wiki mutation {op!r}.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        wiki = _wiki(payload["wiki_id"])
        if op == "move":
            _move(wiki, float(payload["after_lat"]), float(payload["after_lng"]))
            return
        if op == "alias_add":
            alias = WikiAlias.objects.create(
                wiki=wiki,
                name=payload["name"],
                kind=payload.get("kind") or "alternate",
            )
            payload["alias_id"] = alias.pk
            return
        if op == "alias_remove":
            WikiAutoRemoval.objects.record(wiki=wiki, kind=AutoRemovalKind.ALIAS, value=payload["name"])
            WikiAlias.objects.filter(pk=payload.get("alias_id"), wiki=wiki).delete()
            WikiAlias.objects.filter(wiki=wiki, name__iexact=payload["name"]).delete()
            return
        if op == "alias_promote":
            wiki.name = payload["after_name"]
            wiki.save(update_fields=["name", "updated"])
            return
        _expired(f"Unknown wiki mutation {op!r}.")
