"""External-API wrappers around ``dashboard/controllers/pin_bulk.py``'s multi-select actions.

The internal views are plain Django ``View`` subclasses with the ownership
scoping, undo-stashing and merge/reparent logic embedded directly in
``post()`` - there's no extracted service function to call, so each endpoint
here re-implements the same logic against the uuid-addressed, DRF-native
request/response shape the rest of this API uses. See
``serializers_pin_bulk``'s docstring for the two places this deliberately
departs from the internal view's exact field semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import transaction
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
from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.pin_edit import ORGANIZE_LABEL_KINDS, PinReparentError, reparent_pin
from urbanlens.dashboard.services.text_limits import MAX_PIN_DESCRIPTION_LENGTH, text_length_error
from urbanlens.dashboard.services.undo.handlers.pin import MODEL_LABEL as PIN_MODEL_LABEL
from urbanlens.dashboard.services.undo.service import stash_for_undo

if TYPE_CHECKING:
    from rest_framework.request import Request


def _owned_pins(request: Request, uuids: list) -> list[Pin]:
    """The caller's own pins (root or child) among *uuids*, order not guaranteed."""
    return list(Pin.objects.filter(profile__user=request.user, uuid__in=uuids))


class PinBulkDeleteView(ExternalApiView):
    """POST: delete several of the caller's own pins (and their detail-pin subtrees) at once.

    Every pin named must belong to the caller; anything else in ``uuids`` is
    silently ignored rather than refused, so a client replaying a queued
    offline batch doesn't fail the whole request over one pin deleted
    meanwhile on another device. The response's ``undo_uuid`` restores
    everything this call removed via the generic ``POST
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

        subtree = list(Pin.objects.filter(pk__in=[pin.pk for pin in pins]).with_descendants())
        with transaction.atomic():
            # Stashed inside the same atomic block as the delete: a mid-delete
            # failure must roll back both together, never leave a committed
            # UndoAction claiming a deletion that didn't happen.
            undo_action = stash_for_undo(PIN_MODEL_LABEL, subtree, request.user.profile)
            Pin.objects.filter(pk__in=[pin.pk for pin in pins]).delete()

        return Response(
            {
                "deleted": len(pins),
                "descendant_count": len(subtree) - len(pins),
                "total_count": len(subtree),
                "undo_uuid": undo_action.uuid,
            },
        )


class PinBulkMergeView(OwnedPinMixin, ExternalApiView):
    """POST: fold several of the caller's own pins into one target as detail pins.

    A target that's currently itself a detail pin is promoted to top-level
    first - merging always leaves the target as the new top-level pin -
    unless that promotion would collide with another top-level pin already
    sitting at the exact same location, which is refused with 400 rather than
    silently merging two unrelated top-level pins into one.
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

        # Resolved and validated before any write: returning from inside
        # transaction.atomic() without raising commits whatever was already
        # saved rather than rolling it back, so the target's promotion must
        # not happen until we know there's at least one valid source.
        promote_target = target.parent_pin_id is not None
        if promote_target:
            conflict = Pin.objects.filter(profile=target.profile, location_id=target.location_id, parent_pin__isnull=True).exclude(pk=target.pk).exists()
            if conflict:
                return Response({"error": "You already have a top-level pin at this exact location. Choose a different pin as the merge target."}, status=400)

        source_uuids = [str(value) for value in data["source_uuids"]]
        sources = [pin for pin in _owned_pins(request, source_uuids) if pin.pk != target.pk]
        if not sources:
            return Response({"error": "No valid source pins."}, status=400)

        with transaction.atomic():
            if promote_target:
                target.parent_pin = None
                target.save(update_fields=["parent_pin", "updated"])

            merged_uuids: list[str] = []
            skipped_uuids: list[str] = []
            for source in sources:
                # Structurally unreachable once target is root (either already
                # was, or was just promoted above) - kept as defense-in-depth,
                # matching the internal view's own guard.
                if source.would_create_cycle(target):
                    skipped_uuids.append(str(source.uuid))
                    continue
                source.parent_pin = target
                source.save(update_fields=["parent_pin", "updated"])
                merged_uuids.append(str(source.uuid))

            if not merged_uuids:
                return Response({"error": "Merge would create a cycle."}, status=400)

        return Response(
            {
                "target": PinSummarySerializer(target).data,
                "merged_uuids": merged_uuids,
                "skipped_uuids": skipped_uuids,
            },
        )


class PinBulkEditView(ExternalApiView):
    """POST: apply the same description, rating, label, and/or parent change to several pins at once.

    Every field but ``uuids`` is optional and independent - send only the
    ones you're changing. See ``serializers_pin_bulk.PinBulkEditSerializer``
    for exact null/absent semantics.
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

        # Every field is resolved and validated up front, before any write:
        # this endpoint isn't wrapped in a single all-or-nothing transaction
        # (each field's changes are independently meaningful), so a 400
        # raised partway through would otherwise leave earlier fields' edits
        # committed despite the batch as a whole being rejected.
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

        parent: Pin | None = None
        if "parent_uuid" in data and data["parent_uuid"] is not None:
            parent = Pin.objects.filter(uuid=str(data["parent_uuid"]), profile=profile).first()
            if parent is None:
                return Response({"error": "No such pin to set as parent."}, status=400)

        with transaction.atomic():
            if "description" in data:
                description = data["description"]
                for pin in pins:
                    pin.description = description or None
                    pin.save(update_fields=["description", "updated"])

            if "rating" in data:
                rating = data["rating"]
                if rating is None:
                    Review.objects.filter(profile=profile, pin__in=pins).delete()
                else:
                    for pin in pins:
                        Review.objects.update_or_create(profile=profile, pin=pin, defaults={"rating": rating})

            if to_add:
                for pin in pins:
                    pin.labels.add(*to_add)

            if to_remove:
                for pin in pins:
                    present = [label for label in to_remove if pin.labels.filter(pk=label.pk).exists()]
                    if not present:
                        continue
                    for label in present:
                        PinAutoRemoval.objects.record(pin=pin, kind=AutoRemovalKind.LABEL, value=str(label.pk))
                    pin.labels.remove(*present)

            reparented = 0
            if "parent_uuid" in data:
                if parent is None:
                    for pin in pins:
                        if pin.parent_pin_id is None:
                            continue
                        try:
                            reparent_pin(pin, None)
                        except PinReparentError:
                            continue
                        reparented += 1
                else:
                    for pin in pins:
                        if pin.pk == parent.pk or pin.would_create_cycle(parent):
                            continue
                        pin.parent_pin = parent
                        pin.save(update_fields=["parent_pin", "updated"])
                        reparented += 1

        return Response({"count": len(pins), "reparented": reparented})
