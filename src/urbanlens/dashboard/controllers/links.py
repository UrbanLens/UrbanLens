"""Link views - add and remove external website links on Pins and Wikis.

Each link may carry a Wayback Machine snapshot url, filled in asynchronously
(see models.links.signals / tasks.archive_link_to_wayback) shortly after creation.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, WikiAutoRemoval
from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH, PinLink, WikiLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki_edit import WikiEdit
from urbanlens.dashboard.services.pins.pin_subresources import InvalidLinkError, LinkExistsError, create_pin_link, delete_pin_link
from urbanlens.dashboard.services.wiki.concealment import visible_rows
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

logger = logging.getLogger(__name__)

_validate_url = URLValidator(schemes=["http", "https"])


def _clean_link_input(request) -> tuple[str, str] | HttpResponse:
    """Validate and return (name, url) from POST data, or a 400 response.

    ``name`` sanitization happens in PinLink/WikiLink.save() (see _LinkBase),
    not here - this only validates the url, which the model doesn't touch.
    """
    url = (request.POST.get("url") or "").strip()
    name = (request.POST.get("name") or "").strip()
    if not url:
        return HttpResponse("A url is required.", status=400)
    if len(url) > MAX_LINK_URL_LENGTH:
        return HttpResponse(f"That url is too long (max {MAX_LINK_URL_LENGTH:,} characters).", status=400)
    try:
        _validate_url(url)
    except ValidationError:
        return HttpResponse("That doesn't look like a valid http(s) url.", status=400)
    return name, url


def _render_pin_links(request, pin: Pin) -> HttpResponse:
    from urbanlens.dashboard.services.ai.link_extraction import ai_extract_button_context

    return render(
        request,
        "dashboard/partials/pins/_pin_links_row.html",
        {
            "pin": pin,
            "links": pin.links.all(),
            "delete_url_name": "pin.link.delete",
            "row_id": "pin-links-row",
            "owner_slug": pin.slug,
            "show_badge": True,
            **ai_extract_button_context(pin.profile.user, pin.profile, pin),
        },
    )


def _render_wiki_links(request, wiki, profile) -> HttpResponse:
    """Render the wiki links row.

    Takes the profile for the same reason ``aliases._render_location_panel``
    does - see that docstring.
    """
    from urbanlens.dashboard.services.wiki.concealment import conceal_rows, conceal_wiki, concealment_active

    links = wiki.links.all()
    if concealment_active(wiki, profile):
        links = conceal_rows(links, profile)

    return render(
        request,
        "dashboard/partials/pins/_pin_links_row.html",
        {
            "wiki": conceal_wiki(wiki, profile),
            "links": links,
            "delete_url_name": "location.wiki.link.delete",
            "row_id": "wiki-links-row",
            "owner_slug": wiki.location.slug,
            "dialog_id": "wiki-link-add-dialog",
            "show_label": True,
        },
    )


class PinLinksView(LoginRequiredMixin, View):
    """GET: HTMX row listing a pin's links.  POST: add a new link."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return _render_pin_links(request, pin)

    def post(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        try:
            create_pin_link(pin, name=(request.POST.get("name") or ""), url=(request.POST.get("url") or ""))
        except (InvalidLinkError, LinkExistsError) as exc:
            return HttpResponse(exc.safe_message, status=400)
        return _render_pin_links(request, pin)


class PinLinkDeleteView(LoginRequiredMixin, View):
    def delete(self, request, pin_slug, link_id):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        link = get_object_or_404(PinLink, id=link_id, pin=pin)
        delete_pin_link(pin, link)
        return _render_pin_links(request, pin)


class LocationLinksView(LoginRequiredMixin, View):
    """GET: HTMX row listing a wiki's links.  POST: add a new link."""

    def get(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        return _render_wiki_links(request, wiki, profile)

    def post(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        cleaned = _clean_link_input(request)
        if isinstance(cleaned, HttpResponse):
            return cleaned
        name, url = cleaned
        try:
            # A wiki is edited by many people at once, so two of them adding the
            # same url is ordinary rather than exceptional. The unique constraint
            # decides; a duplicate is reported as a 400, and notably writes no
            # WikiEdit - recording an edit that changed nothing would put a
            # phantom entry in the wiki's revision history.
            with transaction.atomic():
                WikiLink.objects.create(wiki=wiki, name=name, url=url, created_by=profile)
        except IntegrityError:
            return HttpResponse("That link is already on this page.", status=400)
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"link_added": {"from": None, "to": url}},
        )
        return _render_wiki_links(request, wiki, profile)


class LocationLinkDeleteView(LoginRequiredMixin, View):
    def delete(self, request, location_slug, link_id):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        link = get_object_or_404(visible_rows(WikiLink.objects.filter(wiki=wiki), wiki, profile), id=link_id)
        link_url = link.url
        # Tombstone first: a plugin panel (Nominatim, EPA) can otherwise
        # recreate this exact link the next time its cache goes stale.
        WikiAutoRemoval.objects.record(wiki=wiki, kind=AutoRemovalKind.LINK, value=link_url)
        link.delete()
        WikiEdit.objects.create(
            wiki=wiki,
            editor=profile,
            changes={"link_removed": {"from": link_url, "to": None}},
        )
        return _render_wiki_links(request, wiki, profile)
