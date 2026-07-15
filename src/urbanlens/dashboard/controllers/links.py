"""Link views - add and remove external website links on Pins and Wikis.

Each link may carry a Wayback Machine snapshot url, filled in asynchronously
(see models.links.signals / tasks.archive_link_to_wayback) shortly after creation.
"""

from __future__ import annotations

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views import View

from urbanlens.dashboard.models.links.model import MAX_LINK_URL_LENGTH, PinLink, WikiLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

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
    return render(
        request,
        "dashboard/partials/pins/_pin_links_row.html",
        {
            "pin": pin,
            "links": pin.links.all(),
            "add_url": "pin.links",
            "delete_url_name": "pin.link.delete",
            "row_id": "pin-links-row",
            "owner_slug": pin.slug,
            "hide_when_empty": True,
        },
    )


def _render_wiki_links(request, wiki) -> HttpResponse:
    return render(
        request,
        "dashboard/partials/pins/_pin_links_row.html",
        {
            "wiki": wiki,
            "links": wiki.links.all(),
            "add_url": "location.wiki.links",
            "delete_url_name": "location.wiki.link.delete",
            "row_id": "wiki-links-row",
            "owner_slug": wiki.location.slug,
        },
    )


class PinLinksView(LoginRequiredMixin, View):
    """GET: HTMX row listing a pin's links.  POST: add a new link."""

    def get(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        return _render_pin_links(request, pin)

    def post(self, request, pin_slug):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        cleaned = _clean_link_input(request)
        if isinstance(cleaned, HttpResponse):
            return cleaned
        name, url = cleaned
        PinLink.objects.create(pin=pin, name=name, url=url)
        return _render_pin_links(request, pin)


class PinLinkDeleteView(LoginRequiredMixin, View):
    def delete(self, request, pin_slug, link_id):
        pin = get_object_or_404(Pin, slug=pin_slug, profile__user=request.user)
        link = get_object_or_404(PinLink, id=link_id, pin=pin)
        link.delete()
        return _render_pin_links(request, pin)


class LocationLinksView(LoginRequiredMixin, View):
    """GET: HTMX row listing a wiki's links.  POST: add a new link."""

    def get(self, request, location_slug):
        _location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        return _render_wiki_links(request, wiki)

    def post(self, request, location_slug):
        _location, wiki, profile = resolve_visible_wiki(request, location_slug)
        cleaned = _clean_link_input(request)
        if isinstance(cleaned, HttpResponse):
            return cleaned
        name, url = cleaned
        WikiLink.objects.create(wiki=wiki, name=name, url=url, created_by=profile)
        return _render_wiki_links(request, wiki)


class LocationLinkDeleteView(LoginRequiredMixin, View):
    def delete(self, request, location_slug, link_id):
        _location, wiki, _profile = resolve_visible_wiki(request, location_slug)
        link = get_object_or_404(WikiLink, id=link_id, wiki=wiki)
        link.delete()
        return _render_wiki_links(request, wiki)
