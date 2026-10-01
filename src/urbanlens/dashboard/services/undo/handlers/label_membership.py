"""Reversible label add/remove on pins, wikis, and photos."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval, WikiAutoRemoval
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import MutationUndoHandler, register

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

MODEL_LABEL = "label_membership"


def _expired(message: str) -> NoReturn:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError

    raise UndoExpiredError(message)


def _target(payload: dict[str, Any], profile: Profile) -> Pin | Wiki | Image:
    """The labelled object, only while ``profile`` may still change its labels - the forward edit's own gate."""
    from urbanlens.dashboard.services.wiki.wiki_access import wiki_accessible_to

    kind = payload.get("target")
    target_id = payload.get("target_id")
    if kind == "pin":
        target: Pin | Wiki | Image | None = Pin.objects.filter(pk=target_id, profile=profile).first()
    elif kind == "wiki":
        wiki = Wiki.objects.filter(pk=target_id).select_related("location", "parent_wiki__location").first()
        target = wiki if wiki is not None and wiki_accessible_to(wiki, profile) else None
    elif kind == "image":
        target = Image.objects.filter(pk=target_id, profile=profile).first()
    else:
        _expired(f"Unknown label target {kind!r}.")
    if target is None:
        _expired("The item this label was on no longer exists.")
    return target


def _label(label_id: int, profile: Profile) -> Label:
    label = Label.objects.visible_to(profile).filter(pk=label_id).first()
    if label is None:
        _expired("This label no longer exists.")
    return label


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
    """Undo/redo label add/remove on a pin, wiki, or photo."""

    model_label = MODEL_LABEL

    @classmethod
    def undo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        target = _target(payload, profile)
        label = _label(payload["label_id"], profile)
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
    def redo_mutation(cls, payload: dict[str, Any], profile: Profile) -> None:
        target = _target(payload, profile)
        label = _label(payload["label_id"], profile)
        if payload.get("op") == "add":
            _add(target, label)
            return
        if payload.get("op") == "remove":
            _remove(target, label, tombstone=True)
            return
        _expired(f"Unknown label mutation {payload.get('op')!r}.")
