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

from urbanlens.dashboard.controllers.pin_edit import _overview_context, _pin_for_user
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.locations.creation import WikiCreationService, seedable_field_values

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
        wiki, created = WikiCreationService().create_for_pin(pin, include_fields=include_fields)
        logger.info("User %s %s wiki %s for location %s from pin %s", request.user.id, "created" if created else "linked to existing", wiki.pk, pin.location_id, pin.pk)

        pin.refresh_from_db()
        created_flag = "true" if created else "false"
        response = render(request, "dashboard/partials/pins/pin_overview_partial.html", _overview_context(pin))
        response["HX-Trigger"] = f'{{"wikiCreated": {{"created": {created_flag}}}}}'
        return response
