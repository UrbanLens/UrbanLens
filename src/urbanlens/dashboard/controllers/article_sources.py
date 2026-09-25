"""Article > Sources: the documents cached for a pin's or wiki's location, and a PDF proxy scoped to them."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING

from csp.decorators import csp_exempt
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View

from urbanlens.dashboard.controllers.article import owned_pin
from urbanlens.dashboard.services.locations.site_scope import is_site_scope
from urbanlens.dashboard.services.pins.external_data import POLL_INTERVAL_SECONDS
from urbanlens.dashboard.services.pins.source_documents import collect_source_documents, find_listed_document, pdf_bytes
from urbanlens.dashboard.services.wiki.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.pins.source_documents import ListedDocument

logger = logging.getLogger(__name__)

#: Longer than the panels' own budget: a campus pass fetches many buildings' records and can run near the task's time limit.
SOURCES_MAX_POLL_ATTEMPTS = 75

#: Served on the PDF itself: it loads nothing, and only this site may frame it.
_DOCUMENT_CSP = "default-src 'none'; frame-ancestors 'self'"

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class SourcesScope:
    """Where a Sources request reads from and whose fetch warms it.

    Attributes:
        location: The location whose cache rows list the documents.
        driver: The pin a fetch runs as, or None when the viewer has none here.
        site_scope: Whether the page describes a parcel/site rather than one building.
        panel_url: This scope's Sources panel endpoint.
        pin_slug: The pin page's slug (pin scope only).
        location_slug: The wiki's location slug (wiki scope only).
    """

    location: Location
    driver: Pin | None
    site_scope: bool
    panel_url: str
    pin_slug: str = ""
    location_slug: str = ""

    def document_url(self, source_key: str, document_id: str) -> str:
        """The scoped proxy URL for one listed document."""
        if self.pin_slug:
            return reverse("pin.article.sources.document", kwargs={"pin_slug": self.pin_slug, "source": source_key, "document_id": document_id})
        return reverse("location.wiki.article.sources.document", kwargs={"location_slug": self.location_slug, "source": source_key, "document_id": document_id})


def resolve_sources_scope(request: HttpRequest, *, pin_slug: str = "", location_slug: str = "") -> SourcesScope:
    """Resolve the requester's pin, or a wiki they can see, into a :class:`SourcesScope`.

    Args:
        request: The current request.
        pin_slug: Selects pin scope: one of the requester's own pins.
        location_slug: Selects wiki scope.

    Returns:
        The scope.

    Raises:
        Http404: The pin is not the requester's, or the wiki is not visible to them.
    """
    if location_slug:
        location, wiki, profile = resolve_visible_wiki(request, location_slug)
        driver = location.pins.filter(profile=profile).select_related("location").first()
        slug = location.ensure_slug()
        return SourcesScope(
            location=location,
            driver=driver,
            # The driver's scope, not the wiki's: its fetch decides what the cached row holds.
            site_scope=is_site_scope(driver) if driver is not None else is_site_scope(wiki),
            panel_url=reverse("location.wiki.article.sources", kwargs={"location_slug": slug}),
            location_slug=slug,
        )
    pin = owned_pin(request, pin_slug)
    slug = pin.ensure_slug()
    return SourcesScope(
        location=pin.location,
        driver=pin,
        site_scope=is_site_scope(pin),
        panel_url=reverse("pin.article.sources", kwargs={"pin_slug": slug}),
        pin_slug=slug,
    )


def _poll_attempt(request: HttpRequest) -> int:
    """Which poll cycle this request is (0 for the initial load)."""
    try:
        return max(int(request.GET.get("attempt", "0")), 0)
    except (TypeError, ValueError):
        return 0


class ArticleSourcesView(LoginRequiredMixin, View):
    """GET <pin>/article/sources/ or <location>/wiki/article/sources/ - the Sources sub-tab's list and viewer.

    Polls itself while a source is still fetching. ``?selected=<source>:<document id>`` opens that document in the viewer.
    """

    def get(self, request: HttpRequest, pin_slug: str = "", location_slug: str = "") -> HttpResponse:
        scope = resolve_sources_scope(request, pin_slug=pin_slug, location_slug=location_slug)
        attempt = _poll_attempt(request)
        # Child pin details off: the parcel's own documents only, not each building's.
        include_children = request.GET.get("children", "1") != "0"
        listing = collect_source_documents(
            scope.location,
            viewer=request.user,
            driver=scope.driver,
            site_scope=scope.site_scope and include_children,
            may_fetch=attempt < SOURCES_MAX_POLL_ATTEMPTS,
        )

        selected = request.GET.get("selected", "")
        entries = [self._entry(scope, listed, selected) for listed in listing.documents]
        entries.extend(self._uploaded_entries(request, scope, selected))
        selected_entry = next((entry for entry in entries if entry["selected"]), None)
        return render(
            request,
            "dashboard/partials/articles/_article_sources.html",
            {
                "entries": entries,
                "selected_entry": selected_entry,
                "pending": listing.pending,
                "panel_url": scope.panel_url,
                "next_attempt": attempt + 1,
                "poll_interval": POLL_INTERVAL_SECONDS,
                "selected": selected if selected_entry else "",
                "site_scope": scope.site_scope,
                "can_upload": bool(scope.pin_slug),
                "children_qs": "" if include_children else "&children=0",
            },
        )

    def post(self, request: HttpRequest, pin_slug: str = "", location_slug: str = "") -> HttpResponse:
        """Upload a document onto this pin and return the Sources list, using the vault upload pipeline."""
        from urbanlens.dashboard.models.profile.model import Profile
        from urbanlens.dashboard.services.photos.photo_upload import PhotoUploadError, upload_photo

        if not pin_slug:
            return HttpResponse(status=404)
        scope = resolve_sources_scope(request, pin_slug=pin_slug, location_slug=location_slug)
        document = request.FILES.get("document")
        if document is None or scope.driver is None:
            return HttpResponse("No document provided.", status=400)
        profile, _ = Profile.objects.get_or_create(user=request.user)
        try:
            upload_photo(profile, document, caption=document.name or "", pin=scope.driver)
        except PhotoUploadError as exc:
            return HttpResponse(exc.generic_message, status=exc.status)
        return self.get(request, pin_slug=pin_slug, location_slug=location_slug)

    @staticmethod
    def _uploaded_entries(request: HttpRequest, scope: SourcesScope, selected: str) -> list[dict]:
        """Documents the viewer uploaded onto this pin."""
        from urbanlens.dashboard.models.images.model import Image

        if scope.driver is None:
            return []
        from urbanlens.dashboard.models.profile.model import Profile

        profile, _ = Profile.objects.get_or_create(user=request.user)
        rows = Image.objects.uploaded_by(profile).documents().filter(pin=scope.driver)
        entries = []
        for row in rows:
            key = f"upload:{row.pk}"
            url = row.display_url
            entries.append(
                {
                    "key": key,
                    "provider": "upload",
                    "provider_title": "Uploaded",
                    "type": "pdf",
                    "url": url,
                    "title": row.caption or "Uploaded document",
                    "subject": "Processing" if not url else "",
                    "building": "",
                    "selected": key == selected and bool(url),
                }
            )
        return entries

    @staticmethod
    def _entry(scope: SourcesScope, listed: ListedDocument, selected: str) -> dict:
        """One document's template row."""
        document = listed.document
        key = f"{listed.source.key}:{document.document_id}"
        return {
            "key": key,
            "provider": listed.source.key,
            "provider_title": listed.source.title,
            "type": "pdf",
            "url": scope.document_url(listed.source.key, document.document_id),
            "title": document.title,
            "subject": document.subject,
            "building": document.subject if document.subject_kind == "building" else "",
            "selected": key == selected,
        }


@method_decorator(csp_exempt(REPORT_ONLY=True), name="dispatch")
class ArticleSourceDocumentView(LoginRequiredMixin, View):
    """GET .../article/sources/<source>/<document id>/ - one listed PDF, framed by the Sources viewer.

    Serves only what this pin's or wiki's own Sources list names, and only bytes that are a PDF.
    """

    def get(self, request: HttpRequest, source: str, document_id: str, pin_slug: str = "", location_slug: str = "") -> HttpResponse:
        scope = resolve_sources_scope(request, pin_slug=pin_slug, location_slug=location_slug)
        listed = find_listed_document(scope.location, source, document_id, viewer=request.user, site_scope=scope.site_scope)
        if listed is None and scope.site_scope:
            listed = find_listed_document(scope.location, source, document_id, viewer=request.user, site_scope=False)
        if listed is None:
            return HttpResponse("This document is no longer in the sources for this pin.", status=404, content_type="text/plain; charset=utf-8")
        content = pdf_bytes(listed)
        if content is None:
            return HttpResponse("This document could not be loaded.", status=404, content_type="text/plain; charset=utf-8")

        response = HttpResponse(content, content_type="application/pdf")
        filename = _UNSAFE_FILENAME.sub("_", listed.document.title).strip("_") or "document"
        response["Content-Disposition"] = f'inline; filename="{filename[:80]}.pdf"'
        response["X-Frame-Options"] = "SAMEORIGIN"
        response["Content-Security-Policy"] = _DOCUMENT_CSP
        response["X-Content-Type-Options"] = "nosniff"
        response["Cross-Origin-Resource-Policy"] = "same-origin"
        response["Cache-Control"] = "private, max-age=3600"
        return response
