"""User-initiated creation of a community wiki from the pin detail page.

GET  /map/pin/<slug>/wiki/create/  → HTMX dialog listing seedable pin fields
POST /map/pin/<slug>/wiki/create/  → create the wiki (seeded with chosen fields)

Wikis are never created automatically; this is the only creation entry point.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.views import View

from urbanlens.dashboard.controllers.pin_edit import _overview_context, _pin_for_user, _pin_hero_oob
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.creation import (
    WikiCreationService,
    seedable_aliases,
    seedable_field_values,
    seedable_photos,
)

logger = logging.getLogger(__name__)


class PinWikiCreateView(LoginRequiredMixin, View):
    """Create the community wiki for a pin's Location, seeded from chosen pin fields."""

    def get(self, request, pin_slug):
        """Render the create-wiki dialog with the pin fields available for seeding."""
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
            "dashboard/partials/pins/pin_wiki_create_dialog.html",
            {
                "pin": pin,
                # Someone may have created it since the page rendered.
                "existing_wiki": Wiki.objects.get_for_location(pin.location),
                "seedable_fields": seedable_field_values(pin),
                "seedable_aliases": seedable_aliases(pin),
                "seedable_photos": seedable_photos(pin),
            },
        )

    def post(self, request, pin_slug):
        """Create the wiki, seeding it with the fields the user selected."""
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
        wiki, created = WikiCreationService().create_for_pin(
            pin,
            include_fields=include_fields,
            alias_ids=alias_ids,
            image_ids=image_ids,
        )
        logger.info("User %s %s wiki %s for location %s from pin %s", request.user.id, "created" if created else "linked to existing", wiki.pk, pin.location_id, pin.pk)

        pin.refresh_from_db()
        created_flag = "true" if created else "false"
        overview_context = _overview_context(pin)
        overview_html = render(request, "dashboard/partials/pins/pin_overview_partial.html", overview_context).content.decode()
        # The Community Wiki box lives in the page hero, outside #pin-overview
        # (this view's own hx-target) - without this OOB swap, the "Create
        # Community Wiki" button stays stuck in its stale pre-creation state
        # (and doesn't turn into a link to the new wiki) until a full reload.
        # Same fix PinOverviewView already needed for the slug-backfill case.
        hero_html = _pin_hero_oob(request, pin, overlapping_location_count=overview_context["overlapping_location_count"])
        response = HttpResponse(overview_html + hero_html)
        response["HX-Trigger"] = f'{{"wikiCreated": {{"created": {created_flag}}}}}'
        return response
