"""External-API wrappers for label priority reorder and bulk delete/edit/convert.

See ``serializers_labels_bulk``'s docstring for the one structural departure
from the internal views this mirrors (``dashboard/controllers/organize.py``,
``dashboard/controllers/labels.py``): every endpoint here resolves and checks
each label's own kind rather than trusting a route-bound ``label_kind``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_labels_bulk import (
    LabelBulkConvertResponseSerializer,
    LabelBulkConvertSerializer,
    LabelBulkDeleteResponseSerializer,
    LabelBulkDeleteSerializer,
    LabelBulkEditResponseSerializer,
    LabelBulkEditSerializer,
    LabelReorderResponseSerializer,
    LabelReorderSerializer,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.labels.meta import KIND_STATUS
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.services.labels.hierarchy import would_create_cycle

if TYPE_CHECKING:
    from rest_framework.request import Request


def _owned_editable_labels(profile, uuids: list) -> list[Label]:
    """The caller's own, non-protected labels among *uuids* - foreign, global, and protected entries silently dropped."""
    return list(Label.objects.filter(profile=profile, is_protected=False, uuid__in=uuids))


class LabelReorderView(ExternalApiView):
    """POST: set the caller's display-priority order across their own tag/category/status labels.

    ``uuids`` is the complete desired order, first = highest priority. Global
    labels named in the request are left untouched (see
    ``LabelReorderResponseSerializer.skipped_global_uuids``) - ``Label.order``
    is a single shared column, and rewriting it for a label everyone sees
    would move it for every other user on the site.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(request=LabelReorderSerializer, responses={200: LabelReorderResponseSerializer, 400: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Reorder the caller's own labels named in ``uuids``."""
        serializer = LabelReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uuids = [str(value) for value in serializer.validated_data["uuids"]]
        profile = request.user.profile

        owned_by_uuid = {str(label.uuid): label for label in Label.objects.for_profile(profile).filter(uuid__in=uuids)}

        total = len(uuids)
        reordered: list[Label] = []
        skipped_global_uuids: list[str] = []
        for index, uuid in enumerate(uuids):
            label = owned_by_uuid.get(uuid)
            if label is None:
                skipped_global_uuids.append(uuid)
                continue
            label.order = total - index
            reordered.append(label)

        if reordered:
            Label.objects.bulk_update(reordered, ["order"])

        return Response({"reordered": len(reordered), "skipped_global_uuids": skipped_global_uuids})


class LabelBulkDeleteView(ExternalApiView):
    """POST: delete several of the caller's own, non-protected labels at once."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(request=LabelBulkDeleteSerializer, responses={200: LabelBulkDeleteResponseSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Delete the caller's own labels named in ``uuids``. Pins carrying them are untouched."""
        serializer = LabelBulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uuids = [str(value) for value in serializer.validated_data["uuids"]]

        labels = _owned_editable_labels(request.user.profile, uuids)
        if not labels:
            return Response({"error": "No matching labels."}, status=404)

        count = len(labels)
        Label.objects.filter(pk__in=[label.pk for label in labels]).delete()
        return Response({"deleted": count})


class LabelBulkEditView(ExternalApiView):
    """POST: apply the same icon/color/description/order and/or parent-hierarchy change to several labels.

    Every field but ``uuids`` is optional and independent. See
    ``serializers_labels_bulk.LabelBulkEditSerializer`` for exact null/absent
    semantics.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(request=LabelBulkEditSerializer, responses={200: LabelBulkEditResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Apply a shared partial edit to the caller's own labels named in ``uuids``."""
        serializer = LabelBulkEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        profile = request.user.profile

        uuids = [str(value) for value in data["uuids"]]
        labels = _owned_editable_labels(profile, uuids)
        if not labels:
            return Response({"error": "No matching labels."}, status=404)

        update_fields: set[str] = set()
        for label in labels:
            if "icon" in data:
                label.icon = data["icon"] or None
                update_fields.add("icon")
            if "color" in data:
                label.color = data["color"] or None
                update_fields.add("color")
            if "description" in data:
                label.description = data["description"] or None
                update_fields.add("description")
            if "order" in data:
                label.order = data["order"]
                update_fields.add("order")
        if update_fields:
            Label.objects.bulk_update(labels, list(update_fields))

        # Unknown parent/child uuids are refused, not dropped - silently
        # building a smaller hierarchy than the client asked for is worse than
        # refusing, matching LabelDetailView.patch's single-label semantics.
        add_parent_uuids = [str(value) for value in data.get("add_parent_uuids") or []]
        if add_parent_uuids:
            parents = list(Label.objects.visible_to(profile).filter(uuid__in=add_parent_uuids))
            if len(parents) != len(set(add_parent_uuids)):
                return Response({"error": "One or more add_parent_uuids do not name a label you can use."}, status=400)
            for label in labels:
                safe_parents = [parent for parent in parents if parent.pk != label.pk and not would_create_cycle(label, [parent.pk])]
                if safe_parents:
                    label.parents.add(*safe_parents)

        add_child_uuids = [str(value) for value in data.get("add_child_uuids") or []]
        if add_child_uuids:
            children = list(Label.objects.visible_to(profile).filter(uuid__in=add_child_uuids))
            if len(children) != len(set(add_child_uuids)):
                return Response({"error": "One or more add_child_uuids do not name a label you can use."}, status=400)
            for child in children:
                safe_labels = [label for label in labels if label.pk != child.pk and not would_create_cycle(child, [label.pk])]
                if safe_labels:
                    child.parents.add(*safe_labels)

        return Response({"count": len(labels)})


class LabelBulkConvertView(ExternalApiView):
    """POST: convert several of the caller's own labels to another kind at once.

    A label already at ``target_kind`` is left untouched. A ``status`` label
    cannot be converted to anything else - the internal UI has no path out of
    ``status`` either (see ``LabelBulkConvertView._resolved_target_kind`` in
    ``controllers/labels.py``) - so those are silently skipped too, alongside
    the usual foreign/global/protected drops.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LABELS_WRITE}),
    }

    @extend_schema(request=LabelBulkConvertSerializer, responses={200: LabelBulkConvertResponseSerializer, 404: ErrorSerializer})
    def post(self, request: Request) -> Response:
        """Convert the caller's own labels named in ``uuids`` to ``target_kind``."""
        serializer = LabelBulkConvertSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        target_kind = data["target_kind"]

        uuids = [str(value) for value in data["uuids"]]
        labels = [label for label in _owned_editable_labels(request.user.profile, uuids) if label.kind not in (KIND_STATUS, target_kind)]
        if not labels:
            return Response({"error": "No matching labels."}, status=404)

        for label in labels:
            label.kind = target_kind
            label.parents.clear()
            label.save(update_fields=["kind"])

        return Response({"converted": len(labels)})
