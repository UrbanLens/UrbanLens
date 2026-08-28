"""Undo handler for MarkupMap.

A markup map is hand-drawn work - shapes, arrows, labels, security indicators
placed one by one - and deleting it cascades away every ``PinMarkup`` annotation
it holds. That drawing time is the most expensive thing any of the undo handlers
protect, and it was the last of the designed follow-ups from the undo coverage
audit.

**Shares are deliberately not restored.** ``MarkupMapShare`` rows cascade with
the map, and recreating them would silently re-expose the map to every past
recipient - the delete severed those relationships, and an undo should bring back
the owner's work, not other people's access to it. The owner can re-share.
Inbound attachments (a comment's or message's ``markup_map`` FK) are ``SET_NULL``
and already nulled by the time the stash could run, so they are out of scope by
construction rather than by choice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.markup.model import MarkupMap, PinMarkup
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

_MAP_FIELDS = ("title", "center_latitude", "center_longitude", "zoom", "layer_mode", "show_borders")

_ANNOTATION_FIELDS = (
    "markup_type",
    "geometry",
    "label",
    "color",
    "stroke_width",
    "border_color",
    "fill_opacity",
    "border_opacity",
    "security_indicator",
)

#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.markup_map import MODEL_LABEL``)
#: instead of hand-typing ``"markup_map"`` - a typo in a hand-typed string only
#: fails at runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "markup_map"


@register
class MarkupMapUndoHandler(UndoHandler):
    """Restores a map's own fields and every annotation drawn on it.

    ``MarkupMap`` carries no unique constraints, so restore never refuses over a
    collision - same reasoning as ``LabelUndoHandler``. The optional links
    (``pin``, ``cloned_from``, ``shared_by``, an annotation's ``layer``) restore
    leniently: all are ``SET_NULL`` on their targets' own deletes, so dropping a
    link whose target is gone matches what would have happened to a live map.
    """

    model_label = MODEL_LABEL
    model = MarkupMap

    @classmethod
    def serialize(cls, instances: Sequence[MarkupMap]) -> list[dict[str, Any]]:
        return [cls._serialize_one(markup_map) for markup_map in instances]

    @classmethod
    def _serialize_one(cls, markup_map: MarkupMap) -> dict[str, Any]:
        return {
            "fields": {name: getattr(markup_map, name) for name in _MAP_FIELDS},
            "profile_id": markup_map.profile_id,
            "pin_id": markup_map.pin_id,
            "cloned_from_id": markup_map.cloned_from_id,
            "shared_by_id": markup_map.shared_by_id,
            "annotations": [
                {
                    "fields": {name: getattr(annotation, name) for name in _ANNOTATION_FIELDS},
                    "profile_id": annotation.profile_id,
                    "layer_id": annotation.layer_id,
                }
                for annotation in markup_map.items.order_by("created")
            ],
        }

    @classmethod
    def describe(cls, instances: Sequence[MarkupMap]) -> str:
        return describe_batch("Markup map", "markup maps", [markup_map.title or "Untitled map" for markup_map in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[MarkupMap]:
        """Recreate the maps and their annotations.

        Raises:
            UndoExpiredError: If the owning profile was deleted during the
                retention window. Nothing else can block - no unique
                constraints, and every link restores leniently.
        """
        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.models.markup.model import CustomLayer
        from urbanlens.dashboard.models.markup.signals import defer_pin_inference_sync
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        for entry in payload:
            if not Profile.objects.filter(pk=entry["profile_id"]).exists():
                raise UndoExpiredError("The profile that owned this markup map no longer exists.")

        def surviving(model: Any, pk: int | None) -> int | None:
            return pk if pk is not None and model.objects.filter(pk=pk).exists() else None

        restored: list[MarkupMap] = []
        for entry in payload:
            markup_map = MarkupMap.objects.create(
                profile_id=entry["profile_id"],
                pin_id=surviving(Pin, entry.get("pin_id")),
                cloned_from_id=surviving(MarkupMap, entry.get("cloned_from_id")),
                shared_by_id=surviving(Profile, entry.get("shared_by_id")),
                **entry["fields"],
            )
            annotations = entry.get("annotations") or []
            # An annotation whose author's account is gone is skipped: its
            # profile FK is CASCADE, so recreating it against a dead pk would
            # raise, and inventing a different author would misattribute it.
            author_ids = {a["profile_id"] for a in annotations}
            live_authors = set(Profile.objects.filter(pk__in=author_ids).values_list("pk", flat=True))
            created_items = PinMarkup.objects.bulk_create(
                [
                    PinMarkup(
                        parent_map=markup_map,
                        profile_id=annotation["profile_id"],
                        layer_id=surviving(CustomLayer, annotation.get("layer_id")),
                        **annotation["fields"],
                    )
                    for annotation in annotations
                    if annotation["profile_id"] in live_authors
                ],
            )
            if created_items:
                # bulk_create fires no post_save, so the per-item pin-inference
                # signals never run - and the map's own created-save defers its
                # resync at a moment when, under autocommit, the annotations may
                # not exist yet. Scheduling it here, after the items, is what
                # guarantees the resync sees the restored drawing.
                defer_pin_inference_sync(markup_map.pk)
            restored.append(markup_map)
        return restored
