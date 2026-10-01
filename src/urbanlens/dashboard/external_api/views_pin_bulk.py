"""External-API endpoints for the map's multi-select actions, over ``services.pins.pin_bulk``.

The web views in ``controllers/pin_bulk.py`` call the same service; only the request and response shapes differ.
See ``serializers_pin_bulk``'s docstring for the two places this API's field semantics depart from the web form's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer, PinSummarySerializer
from urbanlens.dashboard.external_api.serializers_pin_bulk import (
    PinBulkDeleteResponseSerializer,
    PinBulkDeleteSerializer,
    PinBulkEditResponseSerializer,
    PinBulkEditSerializer,
    PinBulkMergeResponseSerializer,
    PinBulkMergeSerializer,
)
from urbanlens.dashboard.external_api.views import ExternalApiView, OwnedPinMixin
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.core.text_limits import MAX_PIN_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.pins.pin_bulk import UNSET, BulkPinEdit, BulkPinError, Unset, bulk_delete_pins, bulk_edit_pins, bulk_merge_under
from urbanlens.dashboard.services.pins.pin_edit import ORGANIZE_LABEL_KINDS

if TYPE_CHECKING:
    from rest_framework.request import Request


def _owned_pins(request: Request, uuids: list) -> list[Pin]:
    """The caller's own pins (root or child) among *uuids*, order not guaranteed."""
    return list(Pin.objects.filter(profile__user=request.user, uuid__in=uuids))


class PinBulkDeleteView(ExternalApiView):
    """POST: delete several of the caller's own pins (and their detail-pin subtrees) at once.

    Every pin named must belong to the caller; anything else in ``uuids`` is silently ignored rather
    than refused, so a client replaying a queued offline batch doesn't fail the whole request over one
    pin deleted meanwhile on another device.
    The response's ``undo_uuid`` restores everything this call removed via the generic ``POST
    undo/{undo_uuid}/restore/`` endpoint.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(request=PinBulkDeleteSerializer, responses={200: PinBulkDeleteResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Delete the caller's own pins named in ``uuids``, staging an undo entry."""
        serializer = PinBulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uuids = [str(value) for value in serializer.validated_data["uuids"]]

        pins = _owned_pins(request, uuids)
        if not pins:
            return Response({"error": "No matching pins."}, status=404)

        result = bulk_delete_pins(request.user.profile, pins)
        return Response(
            {
                "deleted": len(result.deleted),
                "descendant_count": result.descendant_count,
                "total_count": len(result.subtree),
                "undo_uuid": result.undo_action.uuid,
            },
        )


class PinBulkMergeView(OwnedPinMixin, ExternalApiView):
    """POST: fold several of the caller's own pins into one target as detail pins.

    A target that's currently itself a detail pin is promoted to top-level first - merging always leaves
    the target as the new top-level pin - unless that promotion would collide with another top-level pin
    already sitting at the exact same location, which is refused with 400 rather than silently merging
    two unrelated top-level pins into one.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(request=PinBulkMergeSerializer, responses={200: PinBulkMergeResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Merge the caller's own ``source_uuids`` pins into ``target_uuid``."""
        serializer = PinBulkMergeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        target = self.get_owned_pin(request, str(data["target_uuid"]))
        if target is None:
            return Response({"error": "No such pin to merge into."}, status=404)

        source_uuids = [str(value) for value in data["source_uuids"]]
        try:
            result = bulk_merge_under(target, _owned_pins(request, source_uuids))
        except BulkPinError as exc:
            return Response({"error": exc.message}, status=400)
        return Response(
            {
                "target": PinSummarySerializer(target).data,
                "merged_uuids": [str(pin.uuid) for pin in result.merged],
                "skipped_uuids": [str(pin.uuid) for pin in result.skipped],
            },
        )


class PinBulkEditView(ExternalApiView):
    """POST: apply the same description, rating, label, and/or parent change to several pins at once.

    Every field but ``uuids`` is optional and independent - send only the ones you're changing.
    See ``serializers_pin_bulk.PinBulkEditSerializer`` for exact null/absent semantics.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(request=PinBulkEditSerializer, responses={200: PinBulkEditResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Apply a shared partial edit to the caller's own pins named in ``uuids``."""
        serializer = PinBulkEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        uuids = [str(value) for value in data["uuids"]]
        pins = _owned_pins(request, uuids)
        if not pins:
            return Response({"error": "No matching pins."}, status=404)
        profile = request.user.profile

        if data.get("description"):
            length_error = text_length_error(data["description"], MAX_PIN_DESCRIPTION_LENGTH, "Description")
            if length_error:
                return Response({"error": length_error}, status=400)

        add_uuids = [str(value) for value in data.get("add_label_uuids") or []]
        to_add: list[Label] = []
        if add_uuids:
            to_add = list(Label.objects.visible_to(profile).filter(uuid__in=add_uuids, kind__in=ORGANIZE_LABEL_KINDS))
            if len(to_add) != len(set(add_uuids)):
                return Response({"error": "One or more add_label_uuids do not name a label you can use."}, status=400)

        remove_uuids = [str(value) for value in data.get("remove_label_uuids") or []]
        to_remove: list[Label] = []
        if remove_uuids:
            to_remove = list(Label.objects.visible_to(profile).filter(uuid__in=remove_uuids, kind__in=ORGANIZE_LABEL_KINDS))
            if len(to_remove) != len(set(remove_uuids)):
                return Response({"error": "One or more remove_label_uuids do not name a label you can use."}, status=400)

        parent: Pin | None | Literal[Unset.UNSET] = UNSET
        if "parent_uuid" in data:
            parent = None
            if data["parent_uuid"] is not None:
                parent = Pin.objects.filter(uuid=str(data["parent_uuid"]), profile=profile).first()
                if parent is None:
                    return Response({"error": "No such pin to set as parent."}, status=400)

        result = bulk_edit_pins(
            profile,
            pins,
            BulkPinEdit(
                description=data.get("description", UNSET),
                rating=data.get("rating", UNSET),
                add_labels=to_add,
                remove_labels=to_remove,
                parent=parent,
            ),
        )
        return Response({"count": result.count, "reparented": result.reparented})
