"""Reversible label add/remove on pins, wikis, and photos."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval, WikiAutoRemoval
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

MODEL_LABEL = "label_membership"


def _expired(message: str) -> None:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _target(payload: dict[str, Any]) -> Pin | Wiki | Image:
    kind = payload.get("target")
    target_id = payload.get("target_id")
    if kind == "pin":
        target: Pin | Wiki | Image | None = Pin.objects.filter(pk=target_id).first()
    elif kind == "wiki":
        target = Wiki.objects.filter(pk=target_id).first()
    elif kind == "image":
        target = Image.objects.filter(pk=target_id).first()
    else:
        _expired(f"Unknown label target {kind!r}.")
        raise AssertionError
    if target is None:
        _expired("The item this label was on no longer exists.")
    return target


def _label(label_id: int) -> Label:
    label = Label.objects.filter(pk=label_id).first()
    if label is None:
        _expired("This label no longer exists.")
    return label  # type: ignore[return-value]


def _add(target: Pin | Wiki | Image, label: Label) -> None:
    target.labels.add(label)


def _remove(target: Pin | Wiki | Image, label: Label, *, tombstone: bool) -> None:
    if tombstone and isinstance(target, Pin):
        PinAutoRemoval.objects.record(pin=target, kind=AutoRemovalKind.LABEL, value=str(label.pk))
    elif tombstone and isinstance(target, Wiki):
        WikiAutoRemoval.objects.record(wiki=target, kind=AutoRemovalKind.LABEL, value=str(label.pk))
    target.labels.remove(label)


def _clear_tombstone(target: Pin | Wiki | Image, label: Label) -> None:
    if isinstance(target, Pin):
        PinAutoRemoval.objects.filter(pin=target, kind=AutoRemovalKind.LABEL, value=str(label.pk)).delete()
    elif isinstance(target, Wiki):
        WikiAutoRemoval.objects.filter(wiki=target, kind=AutoRemovalKind.LABEL, value=str(label.pk)).delete()


def _delete_if_orphaned(label: Label) -> None:
    if label.pins.exists() or label.wikis.exists() or label.images.exists():
        return
    label.delete()


@register
class LabelMembershipUndoHandler(MutationUndoHandler):
    """Undo/redo adding or removing a label on a pin, wiki, or photo."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any]) -> None:
        target = _target(payload)
        label = _label(payload["label_id"])
        if payload.get("op") == "add":
            _remove(target, label, tombstone=False)
            if payload.get("created_label"):
                _delete_if_orphaned(label)
            return
        if payload.get("op") == "remove":
            _clear_tombstone(target, label)
            _add(target, label)
            return
        _expired(f"Unknown label mutation {payload.get('op')!r}.")

    @classmethod
    def redo_mutation(cls, payload: dict[str, Any]) -> None:
        target = _target(payload)
        label = _label(payload["label_id"])
        if payload.get("op") == "add":
            _add(target, label)
            return
        if payload.get("op") == "remove":
            _remove(target, label, tombstone=True)
            return
        _expired(f"Unknown label mutation {payload.get('op')!r}.")
