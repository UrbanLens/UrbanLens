"""Article controller - Wikipedia-style articles for pins and community wikis.

Every view here serves both hosts through the same class:

- Wiki articles are routed with a ``location_slug`` kwarg and resolved through
  the standard wiki visibility gate (:func:`resolve_visible_wiki`), so they
  are only reachable by users with a pin at that location.
- Pin articles are routed with a ``pin_slug`` kwarg and resolved strictly
  against the requesting user's own pins - a pin article is private and can
  never be seen (or even confirmed to exist) by anyone else.

The article tab, editor, preview, revision history, diff, and restore are all
HTMX partials swapped into the host page's Article/History tabs.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import TYPE_CHECKING

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views import View

from urbanlens.dashboard.models.article.model import Article, ArticleRevision
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.articles import diff_revisions, get_article, render_article, save_article
from urbanlens.dashboard.services.text_limits import MAX_ARTICLE_LENGTH, text_length_error
from urbanlens.dashboard.services.wiki_access import resolve_visible_wiki

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ArticleScope:
    """Everything a view needs to know about the article being operated on.

    Attributes:
        profile: The requesting user's profile.
        article: The existing Article row, or None when nothing saved yet.
        pin: Host pin (pin scope only).
        wiki: Host wiki (wiki scope only).
        location: The wiki's Location (wiki scope only) - used to build URLs.
        is_private: True for a pin article (only its owner ever sees it).
        host_name: Display name of the pin/wiki for headings.
        urls: Named endpoint URLs for templates (view/edit/save/preview/
            history). Per-revision URLs are derived from ``history`` by
            appending ``<revision_id>/`` (and ``restore/``), matching the
            nested route layout.
    """

    profile: Profile
    article: Article | None
    pin: Pin | None
    wiki: Wiki | None
    location: Location | None
    is_private: bool
    host_name: str
    urls: dict[str, str]


def _wiki_urls(location_slug: str) -> dict[str, str]:
    """Build the endpoint URL map for a wiki-hosted article."""
    return {
        "view": reverse("location.wiki.article", kwargs={"location_slug": location_slug}),
        "save": reverse("location.wiki.article.save", kwargs={"location_slug": location_slug}),
        "preview": reverse("location.wiki.article.preview", kwargs={"location_slug": location_slug}),
        "image": reverse("location.wiki.article.image", kwargs={"location_slug": location_slug}),
        "history": reverse("location.wiki.article.history", kwargs={"location_slug": location_slug}),
    }


def _pin_urls(pin_slug: str) -> dict[str, str]:
    """Build the endpoint URL map for a pin-hosted article."""
    return {
        "view": reverse("pin.article", kwargs={"pin_slug": pin_slug}),
        "save": reverse("pin.article.save", kwargs={"pin_slug": pin_slug}),
        "preview": reverse("pin.article.preview", kwargs={"pin_slug": pin_slug}),
        "image": reverse("pin.article.image", kwargs={"pin_slug": pin_slug}),
        "history": reverse("pin.article.history", kwargs={"pin_slug": pin_slug}),
    }


class ArticleViewBase(LoginRequiredMixin, View):
    """Shared host resolution for every article endpoint."""

    def resolve(self, request: HttpRequest, **kwargs) -> ArticleScope:
        """Resolve the host (pin or wiki) and its article for this request.

        Args:
            request: The current request.
            **kwargs: URL kwargs; ``location_slug`` selects wiki scope,
                ``pin_slug`` selects pin scope.

        Returns:
            The populated :class:`ArticleScope`.

        Raises:
            Http404: Host not found or not accessible to the requester.
        """
        if "location_slug" in kwargs:
            location, wiki, profile = resolve_visible_wiki(request, kwargs["location_slug"])
            return ArticleScope(
                profile=profile,
                article=get_article(wiki=wiki),
                pin=None,
                wiki=wiki,
                location=location,
                is_private=False,
                host_name=wiki.name or "this place",
                # ensure_slug: the location may have been resolved by UUID.
                urls=_wiki_urls(location.ensure_slug()),
            )

        pin_slug = kwargs.get("pin_slug")
        pin = Pin.objects.filter(slug=pin_slug, profile__user=request.user).select_related("location").first()
        if pin is None:
            try:
                pin = Pin.objects.filter(uuid=pin_slug, profile__user=request.user).select_related("location").first()
            except (ValueError, ValidationError):
                # pin_slug isn't a UUID at all - same outcome as no match.
                pin = None
        if pin is None:
            raise Http404
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return ArticleScope(
            profile=profile,
            article=get_article(pin=pin),
            pin=pin,
            wiki=None,
            location=None,
            is_private=True,
            host_name=pin.effective_name or "this pin",
            urls=_pin_urls(pin.slug),
        )

    @staticmethod
    def toast(response: HttpResponse, level: str, message: str, *extra_events: str) -> HttpResponse:
        """Attach a toastr notification (and optional extra events) via HX-Trigger.

        Args:
            response: The response to annotate.
            level: toastr level ("success", "error", ...).
            message: Toast body.
            *extra_events: Additional client event names to trigger.

        Returns:
            The same response, for chaining.
        """
        payload: dict[str, object] = {"showToast": {"level": level, "message": message}}
        for event in extra_events:
            payload[event] = True
        response["HX-Trigger"] = json.dumps(payload)
        return response

    def render_panel(self, request: HttpRequest, scope: ArticleScope) -> HttpResponse:
        """Render the article panel (Notion-style: always the editable canvas, never a
        separate read-only view - see frontend/ts/entries/article-wysiwyg.ts) for the
        resolved scope.
        """
        latest_revision = scope.article.revisions.order_by("-created").first() if scope.article else None
        return render(
            request,
            "dashboard/partials/articles/_article_panel.html",
            {
                "scope": scope,
                "article": scope.article,
                "toc": (scope.article.toc if scope.article else []) or [],
                "base_revision_id": latest_revision.id if latest_revision else "",
                "max_length": MAX_ARTICLE_LENGTH,
            },
        )


class ArticlePanelView(ArticleViewBase):
    """The article panel (the Article tab's content) - an always-editable canvas,
    like a Notion page, with no separate read-only mode to click "Edit" out of.

    GET /location/<slug>/wiki/article/  or  /map/pin/<slug>/article/
    """

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        scope = self.resolve(request, **kwargs)
        return self.render_panel(request, scope)


class ArticleSaveView(ArticleViewBase):
    """Persist a new article version.

    POST .../article/save/  with ``content``, ``edit_summary`` and
    ``base_revision_id`` (the latest revision the editor was started from,
    used to detect conflicting edits on community wikis).
    """

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        scope = self.resolve(request, **kwargs)
        content = request.POST.get("content", "")
        edit_summary = request.POST.get("edit_summary", "")
        base_revision_raw = request.POST.get("base_revision_id") or ""

        length_error = text_length_error(content, MAX_ARTICLE_LENGTH, "Article")
        if length_error:
            response = HttpResponse(status=400)
            response["HX-Reswap"] = "none"
            return self.toast(response, "error", length_error)

        # Conflict check: someone else saved while this editor was open. The
        # client keeps the user's text so nothing is lost.
        latest = scope.article.revisions.order_by("-created").first() if scope.article else None
        base_revision_id = int(base_revision_raw) if base_revision_raw.isdigit() else None
        if latest is not None and latest.id != base_revision_id:
            response = HttpResponse(status=409)
            response["HX-Reswap"] = "none"
            return self.toast(
                response,
                "error",
                "This article changed while you were editing. Open History to review the other edit, then copy your text and try again.",
            )

        _article, revision = save_article(
            editor=scope.profile,
            content=content,
            edit_summary=edit_summary,
            pin=scope.pin,
            wiki=scope.wiki,
        )
        scope.article = get_article(pin=scope.pin, wiki=scope.wiki)
        message = "Article saved." if revision else "No changes to save."
        response = self.render_panel(request, scope)
        return self.toast(response, "success", message, "articleSaved")


class ArticlePreviewView(ArticleViewBase):
    """Render (but never persist) editor content for live preview.

    POST .../article/preview/  with ``content``.
    """

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        self.resolve(request, **kwargs)
        content = request.POST.get("content", "")
        if text_length_error(content, MAX_ARTICLE_LENGTH, "Article"):
            content = content[:MAX_ARTICLE_LENGTH]
        rendered = render_article(content)
        return render(
            request,
            "dashboard/partials/articles/_article_preview.html",
            {"rendered_html": rendered.html},
        )


class ArticleImageUploadView(ArticleViewBase):
    """Upload an image to embed inline in an article, from the WYSIWYG editor.

    POST .../article/image/  with an ``image`` file.

    Images are stored as ordinary ``Image`` rows against the article's host
    (pin or wiki) - the same model and validation (size/content-type
    sniffing/malware scan/quota) every other gallery upload goes through -
    so a pasted-in article image is never a lower-scrutiny upload path than
    the Memories or pin/wiki gallery.
    """

    def post(self, request: HttpRequest, **kwargs) -> JsonResponse:
        scope = self.resolve(request, **kwargs)
        image_file = request.FILES.get("image")
        if not image_file:
            return JsonResponse({"error": "No image provided."}, status=400)

        from urbanlens.dashboard.models.images.model import Image, MediaKind
        from urbanlens.dashboard.services.images import compute_checksum, image_upload_error
        from urbanlens.dashboard.services.storage import quota_error_for_upload

        upload_error = image_upload_error(image_file, MediaKind.PHOTO)
        if upload_error:
            message, status = upload_error
            return JsonResponse({"error": message}, status=status)

        quota_error = quota_error_for_upload(scope.profile, image_file.size)
        if quota_error:
            return JsonResponse({"error": quota_error}, status=413)

        checksum = compute_checksum(image_file)
        location = scope.location or (scope.pin.location if scope.pin else None)
        img = Image.objects.create(
            image=image_file,
            pin=scope.pin,
            wiki=scope.wiki,
            location=location,
            profile=scope.profile,
            checksum=checksum,
            file_size=image_file.size,
        )

        from urbanlens.dashboard.services.celery import safely_enqueue_task
        from urbanlens.dashboard.tasks import process_image_upload

        safely_enqueue_task(process_image_upload, img.pk)
        return JsonResponse({"url": request.build_absolute_uri(img.image.url)}, status=201)


def _annotate_deltas(revisions: list[ArticleRevision]) -> list[dict]:
    """Pair each revision (newest first) with its size delta and ordinal.

    Args:
        revisions: Revisions ordered newest first.

    Returns:
        Dicts of {revision, delta, number} where number is 1 for the oldest.
    """
    total = len(revisions)
    rows = []
    for index, revision in enumerate(revisions):
        previous = revisions[index + 1] if index + 1 < total else None
        rows.append({"revision": revision, "delta": revision.size_delta(previous), "number": total - index})
    return rows


class ArticleHistoryView(ArticleViewBase):
    """Revision history list for the History tab.

    GET .../article/history/
    """

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        scope = self.resolve(request, **kwargs)
        revisions = list(scope.article.revisions.select_related("editor__user", "restored_from").order_by("-created")) if scope.article else []
        return render(
            request,
            "dashboard/partials/articles/_article_history.html",
            {
                "scope": scope,
                "article": scope.article,
                "revision_rows": _annotate_deltas(revisions),
            },
        )


class ArticleRevisionView(ArticleViewBase):
    """Diff of one revision against its predecessor.

    GET .../article/history/<revision_id>/
    """

    def get(self, request: HttpRequest, **kwargs) -> HttpResponse:
        scope = self.resolve(request, **kwargs)
        if scope.article is None:
            raise Http404
        revision = get_object_or_404(ArticleRevision, id=kwargs["revision_id"], article=scope.article)
        previous = scope.article.revisions.filter(created__lt=revision.created).order_by("-created").first()
        is_current = not scope.article.revisions.filter(created__gt=revision.created).exists()
        return render(
            request,
            "dashboard/partials/articles/_article_diff.html",
            {
                "scope": scope,
                "article": scope.article,
                "revision": revision,
                "previous": previous,
                "is_current": is_current,
                "diff_rows": diff_revisions(previous.content if previous else "", revision.content),
            },
        )


class ArticleRestoreView(ArticleViewBase):
    """Restore an older revision as the newest version.

    POST .../article/history/<revision_id>/restore/
    """

    def post(self, request: HttpRequest, **kwargs) -> HttpResponse:
        scope = self.resolve(request, **kwargs)
        if scope.article is None:
            raise Http404
        revision = get_object_or_404(ArticleRevision, id=kwargs["revision_id"], article=scope.article)
        _article, new_revision = save_article(
            editor=scope.profile,
            content=revision.content,
            edit_summary=f"Restored version from {revision.created:%b %d, %Y %H:%M}",
            pin=scope.pin,
            wiki=scope.wiki,
            restored_from=revision,
        )
        scope.article = get_article(pin=scope.pin, wiki=scope.wiki)
        revisions = list(scope.article.revisions.select_related("editor__user", "restored_from").order_by("-created")) if scope.article else []
        response = render(
            request,
            "dashboard/partials/articles/_article_history.html",
            {
                "scope": scope,
                "article": scope.article,
                "revision_rows": _annotate_deltas(revisions),
            },
        )
        message = "Article restored to the selected version." if new_revision else "That version is already the current article."
        # "articleChanged" refreshes the read-mode Article tab; the history
        # list itself is the body of this response, so it must NOT also listen
        # for this event (that would double-fetch).
        return self.toast(response, "success", message, "articleChanged")
