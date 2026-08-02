"""External-API routes for pin-list actions beyond CRUD and item membership.

``urls.py`` owns ``lists/`` CRUD and ``lists/{slug}/items/``; the markup-map
action lives here, mirroring how ``urls_pin_extra.py`` splits pin actions away
from the frozen pin CRUD routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer, PinListMarkupMapResponseSerializer
from urbanlens.dashboard.external_api.views import ExternalApiView, _get_pin_list
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.map.map_snapshot import materialize_markup_map
from urbanlens.dashboard.services.pins.pin_list_markup import build_list_markup_snapshot

if TYPE_CHECKING:
    from rest_framework.request import Request


class PinListMarkupMapView(ExternalApiView):
    """POST: create or refresh a markup map showing every pin on one of the caller's lists.

    Wraps the same ``services.pins.pin_list_markup`` calls
    ``controllers.pin_lists.PinListMarkupMapView`` uses, but returns the
    created/refreshed map's uuid rather than a website redirect - there's no
    dedicated "fetch one markup map" read endpoint yet, so a client currently
    treats this uuid as an opaque reference (matching how
    ``SafetyCheckinSerializer.markup_map_uuid`` is served today).
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.LISTS_WRITE}),
    }

    @extend_schema(request=None, responses={200: PinListMarkupMapResponseSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, list_slug: str) -> Response:
        """Build (or refresh) the markup map for one of the caller's own lists."""
        pin_list = _get_pin_list(request, list_slug)
        if pin_list is None:
            return Response({"error": "No such list."}, status=404)

        snapshot = build_list_markup_snapshot(pin_list)
        if snapshot is None:
            return Response({"error": "This list has no pins with map coordinates yet."}, status=400)

        markup_map = materialize_markup_map(request.user.profile, snapshot, existing_map=pin_list.markup_map, context=pin_list)
        if markup_map is None:
            # Only returns None when the snapshot itself is None - unreachable
            # here since that was already checked above, but handled
            # explicitly rather than assumed, matching the internal view.
            return Response({"error": "Unable to create markup map."}, status=500)

        if pin_list.markup_map_id != markup_map.pk:
            pin_list.markup_map = markup_map
            pin_list.save(update_fields=["markup_map", "updated"])

        return Response({"markup_map_uuid": markup_map.uuid})
