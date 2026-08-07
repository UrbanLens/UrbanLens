"""Undo handler for Pin (root pins and their personal detail-pin subtree)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

# Fields restored verbatim on undo. Deliberately excludes uuid/slug/created/updated
# (regenerated fresh by Pin.save()) and the location/profile/wiki/parent_pin FKs
# (handled separately below, since FK columns need their `_id` attname, not the
# relation name, to be passed to Pin.objects.create()).
_RESTORABLE_FIELDS = (
    "name_is_user_provided",
    "name",
    "icon",
    "description",
    "priority",
    "vulnerability",
    "danger",
    "last_visited",
    "unlogged_visit_dismissed",
    "pin_type",
    "pin_type_is_user_provided",
    "restructure_offer_dismissed",
    "color",
    "detail_bg_color",
    "detail_bg_opacity",
    "detail_border_color",
    "detail_border_opacity",
    "date_built",
    "date_abandoned",
    "date_last_active",
    "fences",
    "alarms",
    "cameras",
    "security",
    "signs",
    "vps",
    "plywood",
    "locked",
)


#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.pin import MODEL_LABEL``) instead
#: of hand-typing ``"pin"`` - a typo in a hand-typed string only fails at
#: runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "pin"


@register
class PinUndoHandler(UndoHandler):
    """Restores a pin's own fields, hierarchy position, and labels - not its cascade children.

    Reviews, visit history, notes, markup annotations, aliases, and comments
    are gone the instant the pin is deleted and are not restored.
    """

    model_label = MODEL_LABEL

    @classmethod
    def serialize(cls, instances: Sequence[Pin]) -> list[dict[str, Any]]:
        return [cls._serialize_one(pin) for pin in instances]

    @classmethod
    def _serialize_one(cls, pin: Pin) -> dict[str, Any]:
        fields = {name: getattr(pin, name) for name in _RESTORABLE_FIELDS}
        fields["custom_icon"] = pin.custom_icon.name if pin.custom_icon else None
        return {
            "old_pk": pin.pk,
            "fields": fields,
            "location_id": pin.location_id,
            "profile_id": pin.profile_id,
            "wiki_id": pin.wiki_id,
            "parent_pin_old_pk": pin.parent_pin_id,
            "label_ids": list(pin.labels.values_list("id", flat=True)),
        }

    @classmethod
    def describe(cls, instances: Sequence[Pin]) -> str:
        return describe_batch("Pin", "pins", [p.effective_name for p in instances])

    @classmethod
    def _resolved_parent_pk(cls, entry: dict[str, Any], in_batch: set[int]) -> int | None:
        """The old pk of the parent this entry will end up under, or None if it will be a root pin.

        Three outcomes, matching what :meth:`restore` actually does: no parent
        recorded; a parent that is part of this same batch (relinked to its new
        row); or a parent outside the batch, reattached only if it still exists.
        A parent that has since been deleted leaves the pin at the top level.
        """
        old_parent_pk = entry["parent_pin_old_pk"]
        if not old_parent_pk:
            return None
        if old_parent_pk in in_batch:
            return old_parent_pk
        if Pin.objects.filter(pk=old_parent_pk, profile_id=entry["profile_id"]).exists():
            return old_parent_pk
        return None

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[Pin]:
        """Recreate pins with fresh pks/uuids/slugs, relinking hierarchy and labels.

        Parents are created before their children and the link is set at creation
        rather than in a second pass. That ordering is not cosmetic:
        ``db_pin_unique_location_per_profile`` allows one *root* pin per location
        per profile, so a detail pin created parent-less and adopted afterwards is
        momentarily a root pin, and collides with whatever root pin already stands
        at its location.

        Raises:
            UndoExpiredError: If the profile, location, wiki, or any label this
                batch referenced was independently deleted during the retention
                window, since recreating the row would otherwise fail with an
                uncaught IntegrityError. Also raised when a pin that would come
                back as a root pin finds its location already re-pinned by this
                profile - the same constraint, refused cleanly instead of 500ing.
        """
        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        in_batch = {entry["old_pk"] for entry in payload}

        for entry in payload:
            if not Profile.objects.filter(pk=entry["profile_id"]).exists():
                raise UndoExpiredError("The profile that owned this pin no longer exists.")
            if not Location.objects.filter(pk=entry["location_id"]).exists():
                raise UndoExpiredError("The location this pin pointed at no longer exists.")
            wiki_id = entry["wiki_id"]
            if wiki_id is not None and not Wiki.objects.filter(pk=wiki_id).exists():
                raise UndoExpiredError("The wiki this pin was linked to no longer exists.")
            label_ids = entry["label_ids"]
            if label_ids and Label.objects.filter(pk__in=label_ids).count() != len(set(label_ids)):
                raise UndoExpiredError("One of the labels on this pin no longer exists.")
            # Only root pins are covered by the constraint, so only they can be blocked.
            if cls._resolved_parent_pk(entry, in_batch) is None and Pin.objects.filter(
                location_id=entry["location_id"], profile_id=entry["profile_id"], parent_pin__isnull=True,
            ).exists():
                raise UndoExpiredError("You have pinned this place again since deleting it, so the original can't be restored alongside it.")

        old_to_new: dict[int, Pin] = {}
        restored: list[Pin] = []
        # Parents first: a child created before its parent would have to be adopted
        # afterwards, and would be a root pin in the meantime. Repeated passes rather
        # than a sort, so an arbitrarily deep hierarchy in one batch still resolves.
        pending = list(payload)
        while pending:
            progressed = False
            deferred: list[dict[str, Any]] = []
            for entry in pending:
                parent_old_pk = cls._resolved_parent_pk(entry, in_batch)
                if parent_old_pk in in_batch and parent_old_pk not in old_to_new:
                    deferred.append(entry)
                    continue
                parent_pk = old_to_new[parent_old_pk].pk if parent_old_pk in old_to_new else parent_old_pk
                pin = Pin.objects.create(
                    location_id=entry["location_id"],
                    profile_id=entry["profile_id"],
                    wiki_id=entry["wiki_id"],
                    parent_pin_id=parent_pk,
                    **entry["fields"],
                )
                old_to_new[entry["old_pk"]] = pin
                restored.append(pin)
                progressed = True
            if not progressed:
                # A cycle among parent links, which the schema should make impossible.
                raise UndoExpiredError("This pin's hierarchy can no longer be rebuilt.")
            pending = deferred

        for entry in payload:
            label_ids = entry["label_ids"]
            if label_ids:
                old_to_new[entry["old_pk"]].labels.set(label_ids)

        return restored
