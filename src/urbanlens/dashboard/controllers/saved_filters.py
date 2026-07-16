"""Saved main-map filter combinations."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.gis.geos import Polygon
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.labels.meta import ICON_CATEGORIES
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.filter_criteria import deserialize_criteria, serialize_form_criteria
from urbanlens.dashboard.services.geo import dissolve_polygons
from urbanlens.dashboard.services.pin_list_membership import resync_smart_list
from urbanlens.dashboard.services.saved_filter_cache import get_or_compute_matching_uuids
from urbanlens.dashboard.services.undo.service import stash_for_undo

if TYPE_CHECKING:
    from django.contrib.gis.geos import MultiPolygon

_SECTION_TEMPLATE = "dashboard/partials/pins/_saved_filters_section.html"
_TOOLBAR_TEMPLATE = "dashboard/partials/map/_saved_filters_toolbar.html"
_FORM_DIALOG_TEMPLATE = "dashboard/partials/pin_lists/_saved_filter_form_dialog.html"


def _render_section(request, profile: Profile) -> HttpResponse:
    """Re-render the sidebar's Saved Filters section plus an OOB update of the map's toolbar.

    The bottom-right saved-filters toolbar (main map page only) mirrors the
    same ``profile.saved_filters`` list, so every create/delete response also
    carries an out-of-band swap of it - if the toolbar isn't in the DOM (any
    page other than the map), the extra ``hx-swap-oob`` fragment is simply
    ignored by htmx.
    """
    saved_filters = list(profile.saved_filters.all())
    section_html = render(request, _SECTION_TEMPLATE, {"saved_filters": saved_filters}).content.decode()
    toolbar_html = render(request, _TOOLBAR_TEMPLATE, {"saved_filters": saved_filters, "oob": True}).content.decode()
    return HttpResponse(section_html + toolbar_html)


def _dissolve_regions(search_form: SearchForm) -> dict[str, MultiPolygon | None]:
    """Parse and independently dissolve a submitted form's include/exclude regions.

    Each of ``include_regions``/``exclude_regions`` is dissolved on its own -
    overlapping polygons within the same type merge into one component;
    include and exclude are never merged against each other.

    Args:
        search_form: A validated ``SearchForm`` (``is_valid()`` already called).

    Returns:
        Mapping of both keys to a dissolved MultiPolygon, or None when that
        side had no polygons.
    """
    result: dict[str, MultiPolygon | None] = {}
    for key in ("include_regions", "exclude_regions"):
        parsed = search_form.parse_region_geojson(key)
        if not parsed:
            result[key] = None
            continue
        dissolved = dissolve_polygons([sub for sub in parsed if isinstance(sub, Polygon)])
        result[key] = dissolved if len(dissolved) else None
    return result


class SavedFilterCreateView(LoginRequiredMixin, View):
    """Save the main map's current filter state under a name.

    POST /saved-filters/create/ → re-renders the sidebar's Saved Filters section.

    Reads the same POST fields ``SearchForm`` reads (submitted via
    ``hx-include="#filter-form"`` on the map page, or directly by the Filters
    tab's create dialog) so the criteria stored are derived through the exact
    same validation/parsing pipeline the map search endpoint uses.
    """

    def post(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)

        name = (request.POST.get("filter_name") or "").strip()
        if not name:
            return HttpResponse("A name is required to save a filter.", status=400)
        if SavedFilter.objects.filter(profile=profile, name=name).exists():
            return HttpResponse("You already have a saved filter with that name.", status=409)

        search_form = SearchForm(request.POST, profile=profile)
        if not search_form.is_valid():
            return HttpResponse("Invalid filter criteria.", status=400)

        cleaned = dict(search_form.cleaned_data)
        label_groups = search_form.parse_label_groups()
        custom_field_criteria = search_form.parse_custom_field_criteria()
        regions = _dissolve_regions(search_form)
        criteria = serialize_form_criteria(cleaned, label_groups, custom_field_criteria, regions)
        if not criteria:
            return HttpResponse("No active filters to save.", status=400)

        icon = (request.POST.get("icon") or "bookmark").strip()
        SavedFilter.objects.create(profile=profile, name=name, icon=icon, criteria=criteria, order=profile.saved_filters.count())
        return _render_section(request, profile)


class SavedFilterEditView(LoginRequiredMixin, View):
    """Render the Filters tab's create/edit dialog, and save edits.

    GET /saved-filters/new/ (no uuid) → blank create-dialog body.
    GET /saved-filters/<uuid>/edit/ → same dialog body, pre-filled from the
    existing filter. Both share one template so "New Filter" and "Edit" are
    visually identical; the form's submit target differs (this view's own
    POST vs. ``SavedFilterCreateView``) based on whether a filter was given.

    POST /saved-filters/<uuid>/edit/ → same validation/parsing pipeline as
    ``SavedFilterCreateView``, but updates the existing instance in place.
    Returns JSON ``{"ok": true}`` on success.

    Any ``PinList`` still pointing at this filter (``PinList.source_saved_filter``,
    set by ``PinListEditView`` whenever a list is pointed at a SavedFilter) gets
    its ``smart_filter`` snapshot refreshed and membership resynced too -
    otherwise a list would silently drift out of sync the moment its source
    filter's criteria changed, since ``smart_filter`` is normally a one-time
    copy, not a live reference.
    """

    def get(self, request, filter_uuid=None):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        saved_filter = None
        criteria: dict = {}
        initial: dict = {}
        if filter_uuid is not None:
            saved_filter = get_object_or_404(SavedFilter, uuid=filter_uuid, profile=profile)
            criteria = deserialize_criteria(saved_filter.criteria, profile)
            initial = {
                "tags": [label.pk for label in criteria.get("tags", [])],
                "exclude_tags": [label.pk for label in criteria.get("exclude_tags", [])],
                "has_visits": criteria.get("has_visits", ""),
            }
        search_form = SearchForm(profile=profile, initial=initial)
        custom_field_values = {c["field"].pk: c for c in criteria.get("custom_fields", [])}
        return render(
            request,
            _FORM_DIALOG_TEMPLATE,
            {
                "saved_filter": saved_filter,
                "criteria": criteria,
                "form": search_form,
                "custom_field_values": custom_field_values,
                "has_label_groups": bool(saved_filter.criteria.get("label_groups")) if saved_filter else False,
                "selected_tag_ids": initial.get("tags", []),
                "selected_exclude_tag_ids": initial.get("exclude_tags", []),
                "icon_categories": ICON_CATEGORIES,
                "current_icon": saved_filter.icon if saved_filter else "bookmark",
            },
        )

    def post(self, request, filter_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        saved_filter = get_object_or_404(SavedFilter, uuid=filter_uuid, profile=profile)

        name = (request.POST.get("filter_name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "A name is required to save a filter."}, status=400)
        if SavedFilter.objects.filter(profile=profile, name=name).exclude(pk=saved_filter.pk).exists():
            return JsonResponse({"ok": False, "error": "You already have a saved filter with that name."}, status=409)

        search_form = SearchForm(request.POST, profile=profile)
        if not search_form.is_valid():
            return JsonResponse({"ok": False, "error": "Invalid filter criteria."}, status=400)

        cleaned = dict(search_form.cleaned_data)
        label_groups = search_form.parse_label_groups()
        custom_field_criteria = search_form.parse_custom_field_criteria()
        regions = _dissolve_regions(search_form)
        criteria = serialize_form_criteria(cleaned, label_groups, custom_field_criteria, regions)
        if not criteria:
            return JsonResponse({"ok": False, "error": "No active filters to save."}, status=400)

        saved_filter.name = name
        saved_filter.icon = (request.POST.get("icon") or "bookmark").strip()
        saved_filter.criteria = criteria
        saved_filter.save(update_fields=["name", "icon", "criteria", "updated"])

        for pin_list in saved_filter.derived_pin_lists.all():
            pin_list.smart_filter = criteria
            pin_list.save(update_fields=["smart_filter", "updated"])
            resync_smart_list(pin_list)

        return JsonResponse({"ok": True, "uuid": str(saved_filter.uuid)})


class SavedFilterSuggestNameView(LoginRequiredMixin, View):
    """Suggest a filter name from whatever criteria the create/edit dialog currently holds.

    POST /saved-filters/suggest-name/ → JSON ``{"name": str | None}``

    Read-only preview, called from the dialog's JS as the user builds a
    filter, so the "Filter name" field can pre-fill itself (e.g. "4★+ · 2 tags
    included") unless the user has typed their own name. Reads the same
    ``SearchForm``-shaped POST fields the create/edit endpoints read, so the
    suggestion always matches what would actually be saved. Returns
    ``{"name": None}`` on an invalid or still-empty form rather than an error,
    so the caller can just leave the name field alone.
    """

    def post(self, request):
        from urbanlens.dashboard.templatetags.dashboard_tags import filter_criteria_summary

        profile, _ = Profile.objects.get_or_create(user=request.user)
        search_form = SearchForm(request.POST, profile=profile)
        if not search_form.is_valid():
            return JsonResponse({"name": None})

        cleaned = dict(search_form.cleaned_data)
        label_groups = search_form.parse_label_groups()
        custom_field_criteria = search_form.parse_custom_field_criteria()
        regions = _dissolve_regions(search_form)
        criteria = serialize_form_criteria(cleaned, label_groups, custom_field_criteria, regions)
        if not criteria:
            return JsonResponse({"name": None})

        summary = filter_criteria_summary(criteria)
        suggested = summary[:100] if summary and summary != "No conditions set" else None
        return JsonResponse({"name": suggested})


class SavedFilterMatchCountsView(LoginRequiredMixin, View):
    """Live per-filter matching-pin counts for the map toolbar's icon-less filter badges.

    GET /saved-filters/counts/ → JSON ``{"counts": {filter_uuid: count}}``

    An icon-less saved filter's toolbar button shows a live count instead of a
    generic fallback icon (otherwise every icon-less filter looks identical).
    The count is "how many pins would be visible if this filter were turned
    on right now" - the candidate filter combined with the sidebar's own
    ``SearchForm`` criteria AND every OTHER toolbar filter currently active
    (excluding the candidate itself, so an already-active filter's own count
    isn't AND-ed against itself). Reads the same fields ``SearchForm`` and
    ``_apply_toolbar_filters`` read, sent via the same ``hx-include``/params
    the map page already builds for ``map.pins.list``/``map.search``.
    """

    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)

        saved_filters = list(SavedFilter.objects.filter(profile=profile))
        if not saved_filters:
            return JsonResponse({"counts": {}})

        base_query = Pin.objects.filter(profile=profile).root_pins()
        search_form = SearchForm(request.GET, profile=profile)
        if search_form.is_valid():
            criteria = dict(search_form.cleaned_data)
            parsed_groups = search_form.parse_label_groups()
            if parsed_groups is not None:
                criteria["label_groups"] = parsed_groups
            if (custom_field_criteria := search_form.parse_custom_field_criteria()) is not None:
                criteria["custom_fields"] = custom_field_criteria
            criteria["include_regions"] = search_form.parse_region_geojson("include_regions")
            criteria["exclude_regions"] = search_form.parse_region_geojson("exclude_regions")
            base_query = base_query.filter_by_criteria(criteria)

        active_ids = {v for v in request.GET.get("toolbar_filter_ids", "").split(",") if v.strip()}
        active_filters = [f for f in saved_filters if str(f.uuid) in active_ids]

        counts: dict[str, int] = {}
        for candidate in saved_filters:
            query = base_query
            for other in active_filters:
                if other.uuid == candidate.uuid:
                    continue
                query = query.filter(uuid__in=get_or_compute_matching_uuids(profile, other))
            query = query.filter(uuid__in=get_or_compute_matching_uuids(profile, candidate))
            counts[str(candidate.uuid)] = query.count()

        return JsonResponse({"counts": counts})


class SavedFilterDeleteView(LoginRequiredMixin, View):
    """Delete a saved filter.

    POST /saved-filters/<uuid>/delete/ → re-renders the sidebar's Saved Filters section.
    """

    def post(self, request, filter_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        saved_filter = get_object_or_404(SavedFilter, uuid=filter_uuid, profile=profile)
        stash_for_undo("saved_filter", [saved_filter], profile)
        saved_filter.delete()
        response = _render_section(request, profile)
        response["HX-Trigger"] = json.dumps({"showToast": {"level": "success", "message": "Filter deleted. Undo within 7 days from Settings → Undo History."}})
        return response
