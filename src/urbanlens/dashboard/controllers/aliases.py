"""Alias views - add/remove alternate names for Pins and Wikis."""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.aliases.model import PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison

logger = logging.getLogger(__name__)


class PinAliasView(LoginRequiredMixin, View):
    """GET: HTMX panel listing a pin's aliases.  POST: add a new alias."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        aliases = pin.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pins/pin_aliases_panel.html",
            {"pin": pin, "aliases": aliases},
        )

    def post(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        name = (request.POST.get("name") or "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)
        # An alias that only differs from the pin's own name by case, spacing, or
        # punctuation can't add any search value the name doesn't already provide.
        if normalize_name_for_comparison(name) == normalize_name_for_comparison(pin.effective_name):
            return HttpResponse("That alias is too close to the pin's name to improve search results.", status=400)
        try:
            PinAlias.objects.create(pin=pin, name=name)
        except IntegrityError:
            return HttpResponse("That alias already exists.", status=409)
        aliases = pin.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pins/pin_aliases_panel.html",
            {"pin": pin, "aliases": aliases},
        )


class PinAliasDeleteView(LoginRequiredMixin, View):
    def delete(self, request, pin_slug, alias_id):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        get_object_or_404(PinAlias, id=alias_id, pin=pin).delete()
        aliases = pin.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pins/pin_aliases_panel.html",
            {"pin": pin, "aliases": aliases},
        )


def _resolve_wiki(location_slug: str) -> tuple[Location, Wiki]:
    """Resolve the Location for a slug and its (lazily-created) Wiki."""
    location = get_object_or_404(Location, slug=location_slug)
    wiki, _created = Wiki.objects.get_or_create_for_location(location)
    return location, wiki


class LocationAliasView(LoginRequiredMixin, View):
    """GET: HTMX partial listing a wiki's aliases.  POST: add a new alias."""

    def get(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        aliases = wiki.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pins/location_aliases_panel.html",
            {"location": location, "wiki": wiki, "aliases": aliases},
        )

    def post(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        name = (request.POST.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            WikiAlias.objects.create(wiki=wiki, name=name, created_by=profile)
        except IntegrityError:
            return JsonResponse({"ok": False, "error": "That alias already exists."}, status=409)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"alias_added": {"from": None, "to": name}},
        )
        aliases = wiki.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pins/location_aliases_panel.html",
            {"location": location, "wiki": wiki, "aliases": aliases},
        )


class LocationAliasDeleteView(LoginRequiredMixin, View):
    def delete(self, request, location_slug, alias_id):
        location, wiki = _resolve_wiki(location_slug)
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)
        alias_name = alias.name
        alias.delete()
        profile, _ = Profile.objects.get_or_create(user=request.user)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"alias_removed": {"from": alias_name, "to": None}},
        )
        aliases = wiki.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pins/location_aliases_panel.html",
            {"location": location, "wiki": wiki, "aliases": aliases},
        )
