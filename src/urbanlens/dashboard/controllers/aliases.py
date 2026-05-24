"""Alias views — add/remove alternate names for Pins and Locations."""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.aliases.model import LocationAlias, PinAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

logger = logging.getLogger(__name__)


class PinAliasView(LoginRequiredMixin, View):
    """GET: HTMX panel listing a pin's aliases.  POST: add a new alias."""

    def get(self, request, pin_uuid):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        aliases = pin.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pin_aliases_panel.html",
            {"pin": pin, "aliases": aliases},
        )

    def post(self, request, pin_uuid):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        name = (request.POST.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
        try:
            alias = PinAlias.objects.create(pin=pin, name=name)
        except IntegrityError:
            return JsonResponse({"ok": False, "error": "That alias already exists."}, status=409)
        aliases = pin.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pin_aliases_panel.html",
            {"pin": pin, "aliases": aliases},
        )


class PinAliasDeleteView(LoginRequiredMixin, View):
    def delete(self, request, pin_uuid, alias_id):
        pin = get_object_or_404(Pin, uuid=pin_uuid, profile__user=request.user)
        get_object_or_404(PinAlias, id=alias_id, pin=pin).delete()
        aliases = pin.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/pin_aliases_panel.html",
            {"pin": pin, "aliases": aliases},
        )


class LocationAliasView(LoginRequiredMixin, View):
    """GET: HTMX partial listing a location's aliases.  POST: add a new alias."""

    def get(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        aliases = location.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/location_aliases_panel.html",
            {"location": location, "aliases": aliases},
        )

    def post(self, request, location_uuid):
        location = get_object_or_404(Location, uuid=location_uuid)
        name = (request.POST.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
        try:
            profile = request.user.profile
        except Exception:
            profile = None
        try:
            LocationAlias.objects.create(location=location, name=name, created_by=profile)
        except IntegrityError:
            return JsonResponse({"ok": False, "error": "That alias already exists."}, status=409)
        aliases = location.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/location_aliases_panel.html",
            {"location": location, "aliases": aliases},
        )


class LocationAliasDeleteView(LoginRequiredMixin, View):
    def delete(self, request, location_uuid, alias_id):
        location = get_object_or_404(Location, uuid=location_uuid)
        get_object_or_404(LocationAlias, id=alias_id, location=location).delete()
        aliases = location.aliases.order_by("name")
        return render(
            request,
            "dashboard/partials/location_aliases_panel.html",
            {"location": location, "aliases": aliases},
        )
