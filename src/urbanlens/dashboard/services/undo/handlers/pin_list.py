"""Undo handler for PinList.

Deleting a list destroys hand-built curation - which pins, in what order - while
the pins themselves survive. Every comparable delete (pins, wikis, trips, safety
check-ins, saved filters) is already restorable from Undo History; lists were the
gap.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.models.pin_list.model import PinList, PinListItem
from urbanlens.dashboard.services.undo.base import UndoHandler, describe_batch, register

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Plain-JSON fields copied verbatim. ``smart_boundary`` is deliberately not
#: here - it is a GEOS geometry and travels as EWKT (see ``_serialize_one``).
#: ``slug`` is also absent: it is regenerated on save, and reusing the deleted
#: list's slug could collide with a list created since.
_RESTORABLE_FIELDS = ("name", "description", "is_smart", "smart_filter")

#: Registry key for this handler. Exposed as a module-level constant so call
#: sites can import it (``from ...handlers.pin_list import MODEL_LABEL``)
#: instead of hand-typing ``"pin_list"`` - a typo in a hand-typed string only
#: fails at runtime via ``get_handler``'s ``ValueError``.
MODEL_LABEL = "pin_list"


@register
class PinListUndoHandler(UndoHandler):
    """Restores a list's own fields and its membership (pin ids + order).

    The two optional links a list can carry restore leniently rather than
    blocking: ``source_saved_filter`` and ``markup_map`` are both ``SET_NULL``
    on their own deletes, so a restored list simply drops a link whose target
    is gone - exactly what would have happened had the list never been deleted.
    Likewise items whose pin has since been deleted are skipped rather than
    refusing the whole restore: the pins were never part of this deletion, and
    a list of survivors beats no list at all.
    """

    model_label = MODEL_LABEL

    @classmethod
    def serialize(cls, instances: Sequence[PinList]) -> list[dict[str, Any]]:
        return [cls._serialize_one(pin_list) for pin_list in instances]

    @classmethod
    def _serialize_one(cls, pin_list: PinList) -> dict[str, Any]:
        fields = {name: getattr(pin_list, name) for name in _RESTORABLE_FIELDS}
        return {
            "fields": fields,
            "profile_id": pin_list.profile_id,
            # EWKT carries the SRID, so the payload stays JSON-safe without
            # losing the coordinate system.
            "smart_boundary_ewkt": pin_list.smart_boundary.ewkt if pin_list.smart_boundary else None,
            "source_saved_filter_id": pin_list.source_saved_filter_id,
            "markup_map_id": pin_list.markup_map_id,
            "items": [
                {"pin_id": item.pin_id, "order": item.order, "added_via": item.added_via}
                for item in pin_list.items.order_by("order", "created")
            ],
        }

    @classmethod
    def describe(cls, instances: Sequence[PinList]) -> str:
        return describe_batch("List", "lists", [pin_list.name for pin_list in instances])

    @classmethod
    def restore(cls, payload: list[dict[str, Any]]) -> list[PinList]:
        """Recreate the lists and re-add every member pin that still exists.

        Raises:
            UndoExpiredError: If the owning profile was deleted during the
                retention window, or the list's name has since been reused -
                ``uq_pin_list_profile_name`` would otherwise surface as an
                uncaught IntegrityError, the same contract every other handler
                follows. (``uq_pin_list_profile_slug`` cannot fire: the slug is
                regenerated on save rather than restored.)
        """
        from django.contrib.gis.geos import GEOSGeometry

        # Deferred import: services.undo.service imports services.undo.handlers
        # (which imports this module) before UndoExpiredError is defined there.
        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter
        from urbanlens.dashboard.services.undo.service import UndoExpiredError

        for entry in payload:
            if not Profile.objects.filter(pk=entry["profile_id"]).exists():
                raise UndoExpiredError("The profile that owned this list no longer exists.")
            name = entry["fields"].get("name")
            if name and PinList.objects.filter(profile_id=entry["profile_id"], name=name).exists():
                raise UndoExpiredError(f"You already have a list called “{name}”, so this one can't be restored alongside it.")

        restored: list[PinList] = []
        for entry in payload:
            boundary_ewkt = entry.get("smart_boundary_ewkt")
            # The optional links restore only if their target survived - both are
            # SET_NULL on the target's own delete, so dropping them matches what
            # deleting the target would have done to a live list.
            saved_filter_id = entry.get("source_saved_filter_id")
            if saved_filter_id is not None and not SavedFilter.objects.filter(pk=saved_filter_id).exists():
                saved_filter_id = None
            markup_map_id = entry.get("markup_map_id")
            if markup_map_id is not None and not MarkupMap.objects.filter(pk=markup_map_id).exists():
                markup_map_id = None

            pin_list = PinList.objects.create(
                profile_id=entry["profile_id"],
                smart_boundary=GEOSGeometry(boundary_ewkt) if boundary_ewkt else None,
                source_saved_filter_id=saved_filter_id,
                markup_map_id=markup_map_id,
                **entry["fields"],
            )

            items = entry.get("items") or []
            surviving = set(
                Pin.objects.filter(pk__in=[item["pin_id"] for item in items], profile_id=entry["profile_id"]).values_list("pk", flat=True),
            )
            PinListItem.objects.bulk_create(
                [
                    PinListItem(pin_list=pin_list, pin_id=item["pin_id"], order=item["order"], added_via=item["added_via"])
                    for item in items
                    if item["pin_id"] in surviving
                ],
            )
            restored.append(pin_list)
        return restored
