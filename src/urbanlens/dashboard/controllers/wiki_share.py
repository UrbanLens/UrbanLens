"""Sharing a pin's own content to the community wiki for its place.

GET  /map/pin/<slug>/wiki/share/  → HTMX dialog listing shareable pin fields
POST /map/pin/<slug>/wiki/share/  → copy the chosen fields onto the wiki

This used to be the "Create community wiki" button, and creation was the part
of it that mattered least. Every pinned location gets its page automatically,
so what is left is the part that always needed a person: choosing which of your
own notes, names and photos to contribute to a page other people read. That is a
deliberate act - see ``bin/check_pin_not_published_to_wiki.py`` for the bug that
comes from letting it happen as a side effect - so it keeps its own dialog and
its own explicit per-field selection.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.controllers.pin_edit import _overview_context, _pin_for_user, _pin_hero_oob
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.wiki.wiki_share import (
    WikiShareService,
    seedable_aliases,
    seedable_field_values,
    seedable_photos,
)

logger = logging.getLogger(__name__)


class PinWikiShareView(LoginRequiredMixin, View):
    """Share chosen fields from a pin onto the community wiki for its Location."""

    def get(self, request, pin_slug):
        """Render the share dialog with the pin fields available to contribute."""
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        if not request.user.profile.community_enabled:
            return HttpResponse("Community features are disabled for this account.", status=403)
        if pin.location_id is None:
            return HttpResponse("This pin has no shared location.", status=400)

        return render(
            request,
            "dashboard/partials/pins/pin_wiki_share_dialog.html",
            {
                "pin": pin,
                "existing_wiki": Wiki.objects.get_for_location(pin.location),
                "seedable_fields": seedable_field_values(pin),
                "seedable_aliases": seedable_aliases(pin),
                "seedable_photos": seedable_photos(pin),
            },
        )

    def post(self, request, pin_slug):
        """Copy the fields the user selected onto the wiki."""
        result = _pin_for_user(pin_slug, request)
        if isinstance(result, HttpResponse):
            return result
        pin = result

        if not request.user.profile.community_enabled:
            return HttpResponse("Community features are disabled for this account.", status=403)
        if pin.location_id is None:
            return HttpResponse("This pin has no shared location.", status=400)

        include_fields = set(request.POST.getlist("seed_fields"))
        alias_ids = {int(v) for v in request.POST.getlist("alias_ids") if v.isdigit()}
        image_ids = {int(v) for v in request.POST.getlist("image_ids") if v.isdigit()}
        wiki, shared = WikiShareService().share_from_pin(
            pin,
            include_fields=include_fields,
            alias_ids=alias_ids,
            image_ids=image_ids,
        )
        logger.info("User %s shared %s from pin %s to wiki %s (location %s)", request.user.id, "content" if shared else "nothing", pin.pk, wiki.pk, pin.location_id)

        pin.refresh_from_db()
        shared_flag = "true" if shared else "false"
        overview_context = _overview_context(pin)
        overview_html = render(request, "dashboard/partials/pins/pin_overview_partial.html", overview_context).content.decode()
        # The Community Wiki box lives in the page hero, outside #pin-overview
        # (this view's own hx-target), and shows what the pin has contributed -
        # without this OOB swap it stays stale until a full reload. Same fix
        # PinOverviewView already needed for the slug-backfill case.
        hero_html = _pin_hero_oob(request, pin, linked_wiki_locations=overview_context["linked_wiki_locations"])
        response = HttpResponse(overview_html + hero_html)
        response["HX-Trigger"] = f'{{"wikiShared": {{"shared": {shared_flag}}}}}'
        return response
