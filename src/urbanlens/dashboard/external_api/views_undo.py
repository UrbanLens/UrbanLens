"""External-facing undo endpoints: list and restore recently deleted items.

Routed under ``urls_tools.py`` (see that module's docstring): undo is a
utility that belongs to no single resource, aggregating across every model
``services.undo`` can restore. All the actual stash/restore/list logic lives
in ``services.undo.service`` - this module is a thin, scope-aware wrapper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.permissions import credential_grants, filter_sources_by_grants
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_undo import UndoEntrySerializer, UndoHistorySerializer
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.undo.model import UndoAction
from urbanlens.dashboard.services.undo.service import UndoExpiredError, get_undo_history, restore_undo_action

if TYPE_CHECKING:
    from uuid import UUID

    from rest_framework.request import Request

#: Which additional domain scope each undoable ``model_label`` requires,
#: beyond ``undo:read``/``undo:write`` themselves. A credential scoped only
#: for pins should not see - or restore - a safety-checkin deletion it was
#: never granted visibility into.
#: Must name every ``services.undo.handlers`` model label. A label missing here
#: is omitted from the listing (harmless) *and* falls through the restore view's
#: ``.get()`` to no domain scope at all (not harmless) - see
#: ``UndoRestoreView.post``. ``markup_map`` maps onto the pin scopes because a
#: markup map is an annotation layer on a pin's own map and the API has no
#: separate markup scope.
_DOMAIN_READ_SCOPES_BY_MODEL_LABEL: dict[str, ApiKeyScope] = {
    "pin": ApiKeyScope.PINS_READ,
    "wiki": ApiKeyScope.WIKI_READ,
    "trip": ApiKeyScope.TRIPS_READ,
    "saved_filter": ApiKeyScope.LISTS_READ,
    "safety_checkin": ApiKeyScope.SAFETY_READ,
    "pin_list": ApiKeyScope.LISTS_READ,
    "label": ApiKeyScope.LABELS_READ,
    "markup_map": ApiKeyScope.PINS_READ,
    "pin_mutation": ApiKeyScope.PINS_READ,
    "wiki_mutation": ApiKeyScope.WIKI_READ,
    "label_membership": ApiKeyScope.LABELS_READ,
    "photo_mutation": ApiKeyScope.PHOTOS_READ,
}

_DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL: dict[str, ApiKeyScope] = {
    "pin": ApiKeyScope.PINS_WRITE,
    "wiki": ApiKeyScope.WIKI_WRITE,
    "trip": ApiKeyScope.TRIPS_WRITE,
    "saved_filter": ApiKeyScope.LISTS_WRITE,
    "safety_checkin": ApiKeyScope.SAFETY_WRITE,
    "pin_list": ApiKeyScope.LISTS_WRITE,
    "label": ApiKeyScope.LABELS_WRITE,
    "markup_map": ApiKeyScope.PINS_WRITE,
    "pin_mutation": ApiKeyScope.PINS_WRITE,
    "wiki_mutation": ApiKeyScope.WIKI_WRITE,
    "label_membership": ApiKeyScope.LABELS_WRITE,
    "photo_mutation": ApiKeyScope.PHOTOS_WRITE,
}


class UndoListView(ExternalApiView):
    """GET: the caller's recent delete history available to undo.

    Aggregates across every undoable model in one feed. A credential without
    the paired domain-read scope for a given entry's ``model_label`` has that
    entry omitted rather than causing a 403 - ``omitted`` names the dropped
    model_labels so a client can prompt the user to re-authorize rather than
    silently rendering an incomplete list forever. See
    ``external_api.permissions.filter_sources_by_grants``.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.UNDO_READ}),
    }

    @extend_schema(responses={200: UndoHistorySerializer})
    def get(self, request: Request) -> Response:
        """Return the caller's active (non-expired) undo entries, newest first."""
        entries = list(get_undo_history(request.user.profile))
        grants = filter_sources_by_grants(
            request.auth,
            {label: {ApiKeyScope.UNDO_READ, scope} for label, scope in _DOMAIN_READ_SCOPES_BY_MODEL_LABEL.items()},
        )
        visible = [entry for entry in entries if entry.model_label in grants]
        omitted = sorted({entry.model_label for entry in entries if entry.model_label not in grants})
        return Response({"entries": UndoEntrySerializer(visible, many=True).data, "omitted": omitted})


class UndoRestoreView(ExternalApiView):
    """POST: restore one of the caller's undo entries.

    Requires ``undo:write`` and the entry's own domain write scope -
    restoring a delete needs the same authority the delete itself needed.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.UNDO_WRITE}),
    }

    @extend_schema(request=None, responses={200: None, 404: ErrorSerializer, 410: ErrorSerializer})
    def post(self, request: Request, undo_uuid: UUID) -> Response:
        """Restore one of the caller's undo entries.

        Looked up without the ``active()`` filter ``get_undo_history`` applies
        - an already-expired entry must still resolve here so it can answer
        410 rather than 404, distinguishing "this existed and lapsed" from
        "never yours to begin with".
        """
        entry = UndoAction.objects.for_profile(request.user.profile).filter(uuid=undo_uuid).first()
        if entry is None:
            return Response({"error": "No such undo entry."}, status=404)

        domain_scope = _DOMAIN_WRITE_SCOPES_BY_MODEL_LABEL.get(entry.model_label)
        required = {ApiKeyScope.UNDO_WRITE, domain_scope} if domain_scope else {ApiKeyScope.UNDO_WRITE}
        if not credential_grants(request.auth, required):
            # Same no-oracle policy as the rest of this API: a scope gap reads
            # identically to a nonexistent entry, never a 403.
            return Response({"error": "No such undo entry."}, status=404)

        try:
            restore_undo_action(entry)
        except UndoExpiredError:
            return Response({"error": "This undo entry has expired."}, status=410)
        return Response({"restored": True})
