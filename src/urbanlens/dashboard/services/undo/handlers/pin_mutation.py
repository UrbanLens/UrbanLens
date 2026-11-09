"""Reversible pin changes: moves, aliases, and field edits."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NoReturn

from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

MODEL_LABEL = "pin_mutation"

logger = logging.getLogger(__name__)


def _expired(message: str) -> NoReturn:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _pin(pin_id: int, profile: Profile) -> Pin:
    pin = Pin.objects.filter(pk=pin_id, profile=profile).select_related("location", "profile").first()
    if pin is None:
        _expired("This pin no longer exists.")
    return pin


def _move(pin: Pin, latitude: float, longitude: float, subject_id: int | None = None, *, restore_subject: bool = False) -> None:
    from urbanlens.dashboard.services.pins.pin_edit import PinMoveError, move_pin_to_coordinates

    try:
        move_pin_to_coordinates(pin, latitude, longitude)
    except PinMoveError:
        logger.info("Undo/redo move of pin %s rejected: another root pin holds the target location", pin.pk)
        _expired("This move can't be replayed: another pin now occupies that exact location.")
    if restore_subject:
        from urbanlens.dashboard.models.place.model import Place

        # The place may have been deleted since (the FK is SET_NULL): then the re-derived subject stands.
        if subject_id is None or Place.objects.filter(pk=subject_id).exists():
            Pin.objects.filter(pk=pin.pk).update(subject_place_id=subject_id)
            pin.subject_place_id = subject_id
            if subject_id is not None:
                _follow_lineage(pin, subject_id)


def _follow_lineage(pin: Pin, place_id: int) -> None:
    """
    Move a restored subject on to its successor, if the place was retired by a lineage event since the move was recorded.

    Only the lineage handler repoints subjects (ADR34 §5), and it ran for the pin's *then* subject, so an undo that
    brings back an older, since-retired place would otherwise leave the pin on a place nothing will ever repoint.
    A pin whose point falls in no successor of a split keeps the recorded subject, as in ``repoint_subjects``.
    """
    from urbanlens.dashboard.models.place.lifecycle import PlaceLineageEdge
    from urbanlens.dashboard.models.place.model import Place
    from urbanlens.dashboard.services.pins.primary_wiki import repoint_subjects

    successor_ids = list(PlaceLineageEdge.objects.filter(predecessor_id=place_id, erroneous=False).values_list("successor_id", flat=True))
    if not successor_ids:
        return
    predecessor = Place.objects.filter(pk=place_id).first()
    if predecessor is None:
        return
    successors = list(Place.objects.filter(pk__in=successor_ids))
    repoint_subjects(predecessor, successors, contained_only=len(successors) > 1)
    pin.refresh_from_db(fields=["subject_place"])


def _apply_fields(pin: Pin, fields: dict[str, Any]) -> None:
    update = []
    for name, value in fields.items():
        setattr(pin, name, value)
        update.append(name)
    if update:
        pin.save(update_fields=[*update, "updated"])


@register
class PinMutationUndoHandler(MutationUndoHandler):
    """Undo/redo a pin move, alias change, or field edit."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        op = payload.get("op")
        pin = _pin(payload["pin_id"], profile)
        if op == "move":
            _move(pin, float(payload["before_lat"]), float(payload["before_lng"]), payload.get("before_subject_id"), restore_subject="before_subject_id" in payload)
            return
        if op == "fields":
            _apply_fields(pin, payload.get("before") or {})
            return
        if op == "alias_add":
            PinAlias.objects.filter(pk=payload.get("alias_id"), pin=pin).delete()
            return
        if op == "alias_remove":
            alias = PinAlias.objects.create(
                pin=pin,
                name=payload["name"],
                kind=payload.get("kind") or "alternate",
            )
            payload["alias_id"] = alias.pk
            PinAutoRemoval.objects.filter(pin=pin, kind=AutoRemovalKind.ALIAS, value=payload["name"].casefold()).delete()
            return
        if op == "alias_promote":
            pin.name = payload["before_name"]
            pin.name_is_user_provided = True
            pin.save(update_fields=["name", "name_is_user_provided", "updated"])
            return
        _expired(f"Unknown pin mutation {op!r}.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        op = payload.get("op")
        pin = _pin(payload["pin_id"], profile)
        if op == "move":
            _move(pin, float(payload["after_lat"]), float(payload["after_lng"]), payload.get("after_subject_id"), restore_subject="after_subject_id" in payload)
            return
        if op == "fields":
            _apply_fields(pin, payload.get("after") or {})
            return
        if op == "alias_add":
            alias = PinAlias.objects.create(
                pin=pin,
                name=payload["name"],
                kind=payload.get("kind") or "alternate",
            )
            payload["alias_id"] = alias.pk
            return
        if op == "alias_remove":
            PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.ALIAS, value=payload["name"])
            PinAlias.objects.filter(pk=payload.get("alias_id"), pin=pin).delete()
            PinAlias.objects.filter(pin=pin, name__iexact=payload["name"]).delete()
            return
        if op == "alias_promote":
            pin.name = payload["after_name"]
            pin.name_is_user_provided = True
            pin.save(update_fields=["name", "name_is_user_provided", "updated"])
            return
        _expired(f"Unknown pin mutation {op!r}.")
