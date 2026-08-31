"""Site-admin controller for mapping equivalent external-provider tags together.

See ``services.locations.external_tag_groups`` for the resolution/mutation
logic this only calls into. Modeled on ``site_admin_costs.py``'s shape (a
shared permission mixin, a body-context builder, a toast-and-re-render
helper) rather than growing the much larger ``site_admin.py``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.auth.views import redirect_to_login
from django.db.models import Q
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.models.place.external_tag_group import ExternalTagGroup, ExternalTagVocabularyEntry
from urbanlens.dashboard.services.core.numbers import safe_int
from urbanlens.dashboard.services.locations.external_tag_groups import (
    ExternalTagGroupError,
    create_group,
    move_entry,
    set_preferred,
    suggested_clusters,
)

if TYPE_CHECKING:
    from django.http import HttpRequest, HttpResponse, HttpResponseRedirect

logger = logging.getLogger(__name__)

_BODY_PARTIAL = "dashboard/partials/admin/_external_tag_mapping_body.html"


class _ExternalTagsAdminMixin(LoginRequiredMixin, PermissionRequiredMixin):
    """Shared permission gate for the external-tag mapping admin pages."""

    permission_required = "dashboard.view_site_admin"
    raise_exception = True
    request: HttpRequest

    def handle_no_permission(self) -> HttpResponseRedirect:
        """Send anonymous users to login; return 403 for authenticated non-admins."""
        if not self.request.user.is_authenticated:
            return redirect_to_login(
                self.request.get_full_path(),
                login_url=self.get_login_url(),
                redirect_field_name=self.get_redirect_field_name(),
            )
        return super().handle_no_permission()


def _admin_context(*, search: str = "", **extra: Any) -> dict[str, Any]:
    """Build the shared context for the mapping page and its body partial."""
    groups = ExternalTagGroup.objects.non_empty().prefetch_related("members").order_by("pk")
    if search:
        groups = groups.filter(Q(members__key__icontains=search) | Q(members__value__icontains=search)).distinct()

    ungrouped = ExternalTagVocabularyEntry.objects.ungrouped().order_by("source", "key", "value")
    if search:
        ungrouped = ungrouped.filter(Q(key__icontains=search) | Q(value__icontains=search))

    clusters = [cluster for cluster in suggested_clusters() if not search or any(search.lower() in entry.value.lower() or search.lower() in entry.key.lower() for entry in cluster.entries)]
    clustered_ids = {entry.pk for cluster in clusters for entry in cluster.entries}
    singleton_pool = [entry for entry in ungrouped if entry.pk not in clustered_ids]

    context: dict[str, Any] = {
        "groups": groups,
        "clusters": clusters,
        "singleton_pool": singleton_pool,
        "search": search,
        "page_name": "site-admin-external-tags",
        "active": "external_tags",
    }
    context.update(extra)
    return context


def _toast_response(request: HttpRequest, *, level: str, message: str, search: str = "", status: int = 200) -> HttpResponse:
    """Re-render the shared body partial with a toast fired via HX-Trigger."""
    response = render(request, _BODY_PARTIAL, _admin_context(search=search), status=status)
    response["HX-Trigger"] = json.dumps({"showToast": {"level": level, "message": message}})
    return response


class SiteAdminExternalTagsView(_ExternalTagsAdminMixin, View):
    """Admin page for mapping equivalent external-provider tags together.

    GET /site-admin/external-tags/  → full page: explicit groups, suggested
        (auto-matched, unconfirmed) clusters, and the remaining ungrouped pool.
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        search = request.GET.get("q", "").strip()
        return render(request, "dashboard/pages/site_admin_external_tags.html", _admin_context(search=search))


class SiteAdminExternalTagsSearchView(_ExternalTagsAdminMixin, View):
    """Re-render the mapping body for a search-as-you-type query.

    GET /site-admin/external-tags/search/
    """

    def get(self, request: HttpRequest) -> HttpResponse:
        search = request.GET.get("q", "").strip()
        return render(request, _BODY_PARTIAL, _admin_context(search=search))


class SiteAdminExternalTagsGroupView(_ExternalTagsAdminMixin, View):
    """Create a new equivalence group from two or more selected (or suggested) tags.

    POST /site-admin/external-tags/group/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        # safe_int defaults to 0 for anything unparseable, which is never a
        # real pk here - filtering falsy values discards those the same way
        # an explicit "was this present" check would.
        entry_ids = [entry_id for raw in request.POST.getlist("entry_id") if (entry_id := safe_int(raw))]
        raw_preferred = request.POST.get("preferred_id", "").strip()
        preferred_id = safe_int(raw_preferred) if raw_preferred else None
        search = request.POST.get("search", "").strip()

        try:
            create_group(entry_ids, preferred_id=preferred_id)
        except ExternalTagGroupError as exc:
            return _toast_response(request, level="error", message=exc.safe_message, search=search, status=400)

        return _toast_response(request, level="success", message="Tags grouped.", search=search)


class SiteAdminExternalTagsMoveView(_ExternalTagsAdminMixin, View):
    """Drag-and-drop endpoint: move one tag to a different group, or ungroup it.

    POST /site-admin/external-tags/move/

    Unlike the other actions here, this returns plain JSON rather than a
    re-rendered partial - the drop has already moved the chip in the DOM
    client-side (see ``external-tag-mapping.ts``), so only success/failure
    and which now-empty group card (if any) to remove need to come back.
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        from django.http import JsonResponse

        raw_entry_id = request.POST.get("entry_id", "").strip()
        entry_id = safe_int(raw_entry_id) if raw_entry_id else None
        raw_target = request.POST.get("target_group_id", "").strip()
        target_group_id = safe_int(raw_target) if raw_target else None

        if entry_id is None:
            return JsonResponse({"ok": False, "message": "Missing tag id."}, status=400)

        try:
            emptied_group_id = move_entry(entry_id, target_group_id)
        except ExternalTagGroupError as exc:
            return JsonResponse({"ok": False, "message": exc.safe_message}, status=400)

        return JsonResponse({"ok": True, "emptied_group_id": emptied_group_id})


class SiteAdminExternalTagsPreferredView(_ExternalTagsAdminMixin, View):
    """Mark one tag as the member shown for its group.

    POST /site-admin/external-tags/preferred/
    """

    def post(self, request: HttpRequest) -> HttpResponse:
        raw_entry_id = request.POST.get("entry_id", "").strip()
        raw_group_id = request.POST.get("group_id", "").strip()
        entry_id = safe_int(raw_entry_id) if raw_entry_id else None
        group_id = safe_int(raw_group_id) if raw_group_id else None
        search = request.POST.get("search", "").strip()

        if entry_id is None or group_id is None:
            return _toast_response(request, level="error", message="Missing tag or group id.", search=search, status=400)

        try:
            set_preferred(entry_id, group_id)
        except ExternalTagGroupError as exc:
            return _toast_response(request, level="error", message=exc.safe_message, search=search, status=400)

        return _toast_response(request, level="success", message="Preferred tag updated.", search=search)
