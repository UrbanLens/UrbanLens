"""Undo handler for Label.

Deleting a label is more destructive than it looks: besides the label row, the
delete cascades away its position in the hierarchy (both directions of the
``parents`` self-M2M) and its assignment to every pin that carried it. The pins
survive, but silently lose the tag - and with it the icon/colour it may have been
supplying on the map.

REData taxonomy needs no special handling here. Deleting queues a retirement
(``retire_redata_taxonomy_on_delete``), and recreating fires ``post_save``, whose
``sync_redata_taxonomy_on_save`` upserts a fresh definition - the signal pair is
self-healing, which is what makes this handler tractable at all. The parent
relinks below likewise re-trigger definition syncs via the m2m signal, and the
pin re-assignments refresh the server-side map pin cache the same way any label
add does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Plain fields copied verbatim. ``custom_icon`` is an ImageField and travels as
#: its storage name (the file itself is not deleted with the label).
_RESTORABLE_FIELDS = ("name", "description", "color", "icon", "kind", "order", "is_protected", "allow_auto_tag", "keywords")

#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.label import MODEL_LABEL``) instead
#: of hand-typing ``"label"`` - a typo in a hand-typed string only fails at
#: runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "label"


@register
class LabelUndoHandler(UndoHandler):
    """Restores a label's fields, hierarchy links, and pin assignments.

    ``Label`` carries no unique constraints, so - unlike every other handler -
    a restore cannot collide with anything created since. If an identically
    named label now exists the user simply ends up with two, which the organize
    page's merge tool already handles; refusing the restore over it would be
    stricter than the app itself is.

    Everything relational restores leniently: parents, children and pins that
    were deleted since are skipped, because they were never part of this
    deletion and a partial hierarchy beats none.
    """

    model_label = MODEL_LABEL
    model = Label

    @classmethod
    def serialize(cls, instances: Sequence[Label]) -> list[dict[str, Any]]:
        return [cls._serialize_one(label) for label in instances]

    @classmethod
    def _serialize_one(cls, label: Label) -> dict[str, Any]:
        fields = {name: getattr(label, name) for name in _RESTORABLE_FIELDS}
        fields["custom_icon"] = label.custom_icon.name if label.custom_icon else None
        return {
            "old_pk": label.pk,
            "fields": fields,
            "profile_id": label.profile_id,
            "parent_old_pks": list(label.parents.values_list("pk", flat=True)),
            # The reverse direction of the self-M2M: labels that had *this one*
            # as a parent. Those rows die with the delete too, even though the
            # child labels themselves survive.
            "child_old_pks": list(label.children.values_list("pk", flat=True)),
            "pin_ids": list(label.pins.values_list("pk", flat=True)),
        }

    @classmethod
    def describe(cls, instances: Sequence[Label]) -> str:
        return describe_batch("Label", "labels", [label.name for label in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Label]:
        """Recreate the labels, then relink hierarchy and pin assignments.

        Raises:
            UndoExpiredError: If the owning profile was deleted during the
                retention window, or if the label's name has been taken since the
                delete. Every relational piece still restores leniently - a
                parent or pin that vanished meanwhile is skipped rather than
                failing the whole restore.

        The name check exists because ``Label`` gained a uniqueness constraint
        (migration 0042). Before it, restoring onto a reused name simply produced
        a duplicate; now it would raise `IntegrityError` from the database, which
        reaches the user as a 500 with no explanation. Refusing with a message is
        the graceful form of the same answer.
        """
        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        for entry in payload:
            if not Profile.objects.filter(pk=entry["profile_id"]).exists():
                raise UndoExpiredError("The profile that owned this label no longer exists.")

        # Checked for the whole batch before creating any of it, so a collision on
        # the second label does not leave the first restored.
        for entry in payload:
            name = entry["fields"].get("name", "")
            kind = entry["fields"].get("kind", "")
            if Label.objects.filter(profile_id=entry["profile_id"], name__iexact=name, kind=kind).exists():
                raise UndoExpiredError(f'A {kind} called "{name}" already exists, so this one cannot be restored. Rename or merge the existing one first.')

        old_to_new: dict[int, Label] = {}
        restored: list[Label] = []
        for entry in payload:
            label = Label.objects.create(profile_id=entry["profile_id"], **entry["fields"])
            old_to_new[entry["old_pk"]] = label
            restored.append(label)

        for entry, label in zip(payload, restored, strict=True):
            # A parent deleted in this same batch relinks to its restored row;
            # one deleted independently since is skipped.
            parent_pks = [old_to_new[pk].pk if pk in old_to_new else pk for pk in entry["parent_old_pks"]]
            surviving_parents = Label.objects.filter(pk__in=parent_pks, profile_id=entry["profile_id"])
            if surviving_parents:
                label.parents.add(*surviving_parents)

            child_pks = [old_to_new[pk].pk if pk in old_to_new else pk for pk in entry["child_old_pks"]]
            surviving_children = Label.objects.filter(pk__in=child_pks, profile_id=entry["profile_id"])
            for child in surviving_children:
                child.parents.add(label)

            surviving_pins = Pin.objects.filter(pk__in=entry["pin_ids"], profile_id=entry["profile_id"])
            if surviving_pins:
                label.pins.add(*surviving_pins)

        return restored
