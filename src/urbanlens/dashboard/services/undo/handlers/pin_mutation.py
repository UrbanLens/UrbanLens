"""Reversible pin changes: moves, aliases, and field edits."""

from __future__ import annotations

from typing import Any, NoReturn

from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

MODEL_LABEL = "pin_mutation"


def _expired(message: str) -> NoReturn:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _pin(pin_id: int) -> Pin:
    pin = Pin.objects.filter(pk=pin_id).select_related("location", "profile").first()
    if pin is None:
        _expired("This pin no longer exists.")
    return pin


def _move(pin: Pin, latitude: float, longitude: float) -> None:
    from urbanlens.dashboard.services.pins.pin_edit import PinMoveError, move_pin_to_coordinates

    try:
        move_pin_to_coordinates(pin, latitude, longitude)
    except PinMoveError as exc:
        _expired(exc.safe_message)


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
    def undo_mutation(cls, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        pin = _pin(payload["pin_id"])
        if op == "move":
            _move(pin, float(payload["before_lat"]), float(payload["before_lng"]))
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
    def redo_mutation(cls, payload: dict[str, Any]) -> None:
        op = payload.get("op")
        pin = _pin(payload["pin_id"])
        if op == "move":
            _move(pin, float(payload["after_lat"]), float(payload["after_lng"]))
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
