"""Alias views - list, add, remove, and adopt alternate names for Pins and Wikis.

The alias list is the full set of names a pin or place is known by, *including*
the current name (marked in the UI). "Use this name" promotes any other alias
to be the current name; the old name needs no special handling because it is
already an alias.
"""

from __future__ import annotations

import json
import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.aliases.model import AliasType, PinAlias, WikiAlias
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.locations.naming import normalize_name_for_comparison, persist_official_aliases_for_location

logger = logging.getLogger(__name__)


def _show_toast(response: HttpResponse, message: str, level: str = "success") -> HttpResponse:
    """Attach a showToast HX-Trigger to a response.

    Args:
        response: The response to annotate.
        message: Toast message text.
        level: toastr level (``success``, ``info``, ``warning``, ``error``).

    Returns:
        The same response, with the trigger header merged in.
    """
    triggers = json.loads(response.headers.get("HX-Trigger", "{}")) if response.headers.get("HX-Trigger") else {}
    triggers["showToast"] = {"level": level, "message": message}
    response["HX-Trigger"] = json.dumps(triggers)
    return response


def _annotated(aliases, current_name: str | None):
    """Mark each alias with whether it is the current name.

    Args:
        aliases: Alias queryset ordered for display.
        current_name: The pin's/wiki's current display name.

    Returns:
        The aliases as a list, each with an ``is_current`` attribute.
    """
    normalized_current = normalize_name_for_comparison(current_name)
    result = list(aliases)
    for alias in result:
        alias.is_current = bool(normalized_current) and normalize_name_for_comparison(alias.name) == normalized_current
    return result


def _render_pin_panel(request, pin: Pin) -> HttpResponse:
    """Render the pin aliases panel with current-name annotation."""
    aliases = _annotated(pin.aliases.order_by("name"), pin.effective_name)
    return render(
        request,
        "dashboard/partials/pins/pin_aliases_panel.html",
        {"pin": pin, "aliases": aliases},
    )


def _render_location_panel(request, location: Location, wiki: Wiki) -> HttpResponse:
    """Render the wiki aliases panel with current-name annotation."""
    aliases = _annotated(wiki.aliases.order_by("name"), wiki.name)
    return render(
        request,
        "dashboard/partials/pins/location_aliases_panel.html",
        {"location": location, "wiki": wiki, "aliases": aliases},
    )


class PinAliasView(LoginRequiredMixin, View):
    """GET: HTMX panel listing a pin's aliases.  POST: add a new alias."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return _render_pin_panel(request, pin)

    def post(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        name = (request.POST.get("name") or "").strip()
        if not name:
            return HttpResponse("Name is required.", status=400)
        kind = AliasType.NICKNAME if request.POST.get("is_nickname") else AliasType.ALTERNATE
        try:
            PinAlias.objects.create(pin=pin, name=name, kind=kind)
        except IntegrityError:
            return HttpResponse("That alias already exists.", status=409)
        return _render_pin_panel(request, pin)


class PinAliasDeleteView(LoginRequiredMixin, View):
    def delete(self, request, pin_slug, alias_id):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        alias = get_object_or_404(PinAlias, id=alias_id, pin=pin)
        if normalize_name_for_comparison(alias.name) == normalize_name_for_comparison(pin.effective_name):
            return HttpResponse("This alias is the current name - pick another name first.", status=400)
        alias.delete()
        return _render_pin_panel(request, pin)


class PinAliasUseView(LoginRequiredMixin, View):
    """POST: make one of the pin's aliases its current name."""

    def post(self, request, pin_slug, alias_id):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        alias = get_object_or_404(PinAlias, id=alias_id, pin=pin)
        pin.name = alias.name
        pin.name_is_user_provided = True
        pin.save(update_fields=["name", "name_is_user_provided", "updated"])

        from urbanlens.dashboard.controllers.pin_edit import _overview_context

        panel = _render_pin_panel(request, pin)
        overview = render(request, "dashboard/partials/pins/pin_overview_partial.html", {**_overview_context(pin), "oob": True})
        return _show_toast(HttpResponse(panel.content + overview.content), f"Renamed to “{alias.name}”.")


class PinAliasToggleNicknameView(LoginRequiredMixin, View):
    """POST: flip one of the pin's aliases between nickname-only and a plain alias."""

    def post(self, request, pin_slug, alias_id):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        alias = get_object_or_404(PinAlias, id=alias_id, pin=pin)
        alias.toggle_nickname()
        return _render_pin_panel(request, pin)


def _resolve_wiki(location_slug: str) -> tuple[Location, Wiki]:
    """Resolve the Location for a slug and its (lazily-created) Wiki."""
    location = get_object_or_404(Location, slug=location_slug)
    wiki, _created = Wiki.objects.get_or_create_for_location(location)
    return location, wiki


class LocationAliasView(LoginRequiredMixin, View):
    """GET: HTMX partial listing a wiki's aliases.  POST: add a new alias."""

    def get(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        # Wikis are created lazily, so official-name candidates gathered before
        # the wiki existed have no alias rows yet; backfill them from the cache
        # (DB reads only - no network) now that there is a wiki to attach to.
        persist_official_aliases_for_location(location)
        return _render_location_panel(request, location, wiki)

    def post(self, request, location_slug):
        location, wiki = _resolve_wiki(location_slug)
        name = (request.POST.get("name") or "").strip()
        if not name:
            return JsonResponse({"ok": False, "error": "Name is required."}, status=400)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        kind = AliasType.NICKNAME if request.POST.get("is_nickname") else AliasType.ALTERNATE
        try:
            WikiAlias.objects.create(wiki=wiki, name=name, kind=kind, created_by=profile)
        except IntegrityError:
            return JsonResponse({"ok": False, "error": "That alias already exists."}, status=409)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"alias_added": {"from": None, "to": name}},
        )
        return _render_location_panel(request, location, wiki)


class LocationAliasDeleteView(LoginRequiredMixin, View):
    def delete(self, request, location_slug, alias_id):
        location, wiki = _resolve_wiki(location_slug)
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)
        if normalize_name_for_comparison(alias.name) == normalize_name_for_comparison(wiki.name):
            return HttpResponse("This alias is the current name - pick another name first.", status=400)
        alias_name = alias.name
        alias.delete()
        profile, _ = Profile.objects.get_or_create(user=request.user)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"alias_removed": {"from": alias_name, "to": None}},
        )
        return _render_location_panel(request, location, wiki)


class LocationAliasUseView(LoginRequiredMixin, View):
    """POST: make one of the wiki's aliases its current community name."""

    def post(self, request, location_slug, alias_id):
        location, wiki = _resolve_wiki(location_slug)
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)
        previous_name = wiki.name
        wiki.name = alias.name
        wiki.save(update_fields=["name", "updated"])
        profile, _ = Profile.objects.get_or_create(user=request.user)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"name": {"from": previous_name, "to": alias.name}},
        )
        response = _render_location_panel(request, location, wiki)
        response["HX-Trigger"] = json.dumps({"wikiRenamed": {"name": alias.name}})
        return _show_toast(response, f"Renamed to “{alias.name}”.")


class LocationAliasToggleNicknameView(LoginRequiredMixin, View):
    """POST: flip one of the wiki's aliases between nickname-only and a plain alias."""

    def post(self, request, location_slug, alias_id):
        location, wiki = _resolve_wiki(location_slug)
        alias = get_object_or_404(WikiAlias, id=alias_id, wiki=wiki)
        alias.toggle_nickname()
        return _render_location_panel(request, location, wiki)
