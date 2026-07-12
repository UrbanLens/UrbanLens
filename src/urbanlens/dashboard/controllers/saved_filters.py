"""Saved main-map filter combinations."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.forms.search import SearchForm
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.saved_filter.model import SavedFilter
from urbanlens.dashboard.services.filter_criteria import serialize_form_criteria

_SECTION_TEMPLATE = "dashboard/partials/pins/_saved_filters_section.html"


def _render_section(request, profile: Profile) -> HttpResponse:
    saved_filters = list(profile.saved_filters.all())
    return render(request, _SECTION_TEMPLATE, {"saved_filters": saved_filters})


class SavedFilterCreateView(LoginRequiredMixin, View):
    """Save the main map's current filter state under a name.

    POST /saved-filters/create/ → re-renders the sidebar's Saved Filters section.

    Reads the same POST fields ``SearchForm`` reads (submitted via
    ``hx-include="#filter-form"``) so the criteria stored are derived through
    the exact same validation/parsing pipeline the map search endpoint uses.
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
        badge_groups = search_form.parse_badge_groups()
        custom_field_criteria = search_form.parse_custom_field_criteria()
        criteria = serialize_form_criteria(cleaned, badge_groups, custom_field_criteria)
        if not criteria:
            return HttpResponse("No active filters to save.", status=400)

        SavedFilter.objects.create(profile=profile, name=name, criteria=criteria, order=profile.saved_filters.count())
        return _render_section(request, profile)


class SavedFilterDeleteView(LoginRequiredMixin, View):
    """Delete a saved filter.

    POST /saved-filters/<uuid>/delete/ → re-renders the sidebar's Saved Filters section.
    """

    def post(self, request, filter_uuid):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        saved_filter = get_object_or_404(SavedFilter, uuid=filter_uuid, profile=profile)
        saved_filter.delete()
        return _render_section(request, profile)
