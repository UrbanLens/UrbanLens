"""External-API access to a pin's own long-form article and its revision history.

A pin can carry an article exactly as a community wiki can - same ``Article``
row, same Markdown pipeline, same append-only ``ArticleRevision`` history - but
the two are completely different kinds of content, and this module exists
mostly to keep that difference from being lost.

**Scopes are ``pins:read``/``pins:write``, never ``wiki:*``.** A pin article is
the owner's private write-up of their own pin: route notes, access details,
things they specifically did not publish to the community page. ``wiki:write``
means "edit community wikis on your behalf" - a scope a user grants to a
wiki-editing client with no expectation that it can also read their private
notes. Granting a pin article under it would be a privacy bug, not a
convenience, and it would be invisible on the consent screen. The four views
below mirror the wiki article views closely *except* here.

**Nothing here re-implements article logic.** Every underlying service already
takes a ``pin=`` argument (``get_article``, ``save_article_checked``,
``restore_revision`` via ``scope_article``), so this module is pure exposure:
resolve the caller's own pin, hand it to the service, render the shared
payload. The payload itself is ``services.wiki.articles.article_payload``, used by
both hosts so the pin and wiki article bodies cannot drift into disagreeing
about, say, whether ``base_revision_id`` is present.

**Revision ids are always scoped to the resolved article** - a bare
``ArticleRevision.objects.get(id=...)`` would make every revision of every
article in the database reachable by counting integers, including the private
ones this module is careful about.

Image upload is deliberately out of scope for v1: the internal editor's image
flow involves multipart upload, an async malware scan and per-image visibility
rules, and stubbing a simplified version of it here would be a second
implementation of that gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_wiki import (
    ArticleDetailSerializer,
    ArticleRevisionDetailSerializer,
    ArticleRevisionSerializer,
    ArticleSaveSerializer,
)
from urbanlens.dashboard.external_api.views import ExternalApiView, OwnedPinMixin
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.article.model import ArticleRevision
from urbanlens.dashboard.services.wiki.articles import (
    ArticleConflictError,
    article_payload,
    diff_revisions,
    get_article,
    restore_revision,
    save_article_checked,
)
from urbanlens.dashboard.services.wiki.wiki_detail import masked_editor_name

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.article.model import Article
    from urbanlens.dashboard.models.pin.model import Pin


class OwnedPinRequiredMixin(OwnedPinMixin):
    """``OwnedPinMixin`` with the "...or 404" step folded in.

    ``OwnedPinMixin.get_owned_pin_lite`` returns None for both "no such pin"
    and "not yours", leaving every caller to turn that into the uniform 404.
    That is three lines each caller has to remember, and the failure mode of
    forgetting is not a crash: ``None`` flows on into a queryset filter, which
    matches nothing, and the endpoint answers an empty-but-successful body for
    a pin the caller has no right to - a bug that looks like working code.

    Note:
        This belongs on ``OwnedPinMixin`` itself, in ``external_api.views``, so
        the whole package shares it (``views_wiki._own_pin_or_404`` is a third
        copy of the same four lines). It lives here because that module was not
        in this change's scope.

        TODO: move ``get_owned_pin_or_404`` onto ``views.OwnedPinMixin`` and
        repoint this class and ``views_wiki._own_pin_or_404`` at it.
    """

    def get_owned_pin_or_404(self, request: Request, pin_slug: str) -> Pin:
        """Resolve one of the caller's own pins, or raise the uniform 404.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug or uuid, from the URL.

        Returns:
            The caller's matching pin, with ``location`` and ``profile``
            already joined.

        Raises:
            Http404: No such pin, or it belongs to someone else - deliberately
                the same response for both, so a pin slug cannot be used to
                test whether another user has pinned a place.
        """
        pin = self.get_owned_pin_lite(request, pin_slug)
        if pin is None:
            raise Http404
        return pin


class PinArticleApiView(OwnedPinRequiredMixin, ExternalApiView):
    """Base for the pin-article endpoints: own-pin resolution plus article lookup.

    :meth:`pin_article` is the module's whole authorization story, and it is
    deliberately shaped as a *lookup* rather than a check that could answer 403
    - see the module docstring.
    """

    def pin_article(self, request: Request, pin_slug: str) -> tuple[Pin, Article]:
        """Resolve the caller's pin and the article on it, or 404.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug or uuid, from the URL.

        Returns:
            Tuple of (pin, its article).

        Raises:
            Http404: The pin is not the caller's, or it has no article yet.
                A pin with no article is a 404 rather than an empty body so
                that "not written yet" and "not yours" stay one response; the
                PUT handler creates on first save, so a client is never stuck.
        """
        pin = self.get_owned_pin_or_404(request, pin_slug)
        article = get_article(pin=pin)
        if article is None:
            raise Http404
        return pin, article


class PinArticleView(PinArticleApiView):
    """GET the pin's article; PUT to save a new version of it."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
        "PUT": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(responses={200: ArticleDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return the pin's article, body included.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug or uuid.

        Returns:
            200 with the full article payload.
        """
        _pin, article = self.pin_article(request, pin_slug)
        return Response(article_payload(article, request.user.profile))

    @extend_schema(request=ArticleSaveSerializer, responses={200: ArticleDetailSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def put(self, request: Request, pin_slug: str) -> Response:
        """Save a new version of the pin's article, refusing to clobber a concurrent edit.

        Creates the article on first save - which is why this resolves the pin
        rather than the article, and is the escape hatch for the 404 GET
        returns on a pin that has never been written to.

        Args:
            request: The authenticated request carrying the new content.
            pin_slug: The pin's slug or uuid.

        Returns:
            200 with the saved article, or 409 when someone else (the owner on
            another device, or another client holding the same key) saved in
            the meantime. Nothing is written on a 409.
        """
        pin = self.get_owned_pin_or_404(request, pin_slug)

        serializer = ArticleSaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            with transaction.atomic():
                article, _revision = save_article_checked(
                    editor=request.user.profile,
                    content=data["content"],
                    edit_summary=data.get("edit_summary", ""),
                    base_revision_id=data["base_revision_id"],
                    pin=pin,
                )
        except ArticleConflictError as exc:
            # Same contract as the wiki article endpoint and the internal
            # editor: refuse the write and hand back which revision is actually
            # current, so the client can fetch it, show the difference, and
            # re-base rather than guessing.
            return Response(
                {"error": "This article changed while you were editing.", "conflict": True, "current_revision_id": exc.current_revision_id},
                status=409,
            )

        return Response(article_payload(article, request.user.profile))


class PinArticleRevisionsView(PaginatedListMixin, PinArticleApiView):
    """GET the pin article's revision history, newest first."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
    }

    @extend_schema(responses={200: ArticleRevisionSerializer(many=True), 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str) -> Response:
        """Return one page of the article's revisions.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug or uuid.

        Returns:
            A paginated envelope of revision summaries, newest first.
        """
        _pin, article = self.pin_article(request, pin_slug)
        profile = request.user.profile

        revisions = list(article.revisions.select_related("editor__user", "restored_from").order_by("-created", "-pk"))
        rows = [
            {
                "id": revision.pk,
                "edit_summary": revision.edit_summary,
                "editor": masked_editor_name(revision.editor, profile),
                # Delta against the revision immediately before this one;
                # `revisions` is newest-first, so that is the next item.
                "size_delta": revision.size_delta(revisions[index + 1] if index + 1 < len(revisions) else None),
                "restored_from": revision.restored_from_id,
                "created": revision.created.isoformat(),
            }
            for index, revision in enumerate(revisions)
        ]
        return self.paginated_response(rows, ArticleRevisionSerializer, request)


class PinArticleRevisionDetailView(PinArticleApiView):
    """GET one pin-article revision, with its diff against the preceding one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.PINS_READ}),
    }

    @extend_schema(responses={200: ArticleRevisionDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, pin_slug: str, revision_id: int) -> Response:
        """Return one revision's content plus a diff against its predecessor.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug or uuid.
            revision_id: The revision to read.

        Returns:
            200 with the revision's content and its row-wise diff.
        """
        _pin, article = self.pin_article(request, pin_slug)
        profile = request.user.profile

        # article= in the lookup, never a bare id: revision ids are sequential
        # across every article in the database, so an unscoped get would expose
        # other users' private pin articles one integer at a time.
        revision = get_object_or_404(ArticleRevision, id=revision_id, article=article)
        previous = article.revisions.filter(created__lt=revision.created).order_by("-created", "-pk").first()

        return Response(
            {
                "id": revision.pk,
                "edit_summary": revision.edit_summary,
                "editor": masked_editor_name(revision.editor, profile),
                "size_delta": revision.size_delta(previous),
                "restored_from": revision.restored_from_id,
                "created": revision.created.isoformat(),
                "content": revision.content,
                # DiffRow carries only kind ("context"/"add"/"del"/"skip") and
                # the line text - there are no line numbers to expose.
                "diff": [{"kind": row.kind, "text": row.text} for row in diff_revisions(previous.content if previous is not None else "", revision.content)],
            },
        )


class PinArticleRevisionRestoreView(PinArticleApiView):
    """POST to restore an older revision of the pin's article as the newest one."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(request=None, responses={200: ArticleDetailSerializer, 404: ErrorSerializer})
    def post(self, request: Request, pin_slug: str, revision_id: int) -> Response:
        """Restore one revision's content, appending it as a new revision.

        History is append-only: nothing is deleted, the old content is written
        forward and tagged ``restored_from`` so the lineage stays visible.

        Args:
            request: The authenticated request.
            pin_slug: The pin's slug or uuid.
            revision_id: The revision whose content to restore.

        Returns:
            200 with the article as it now stands.
        """
        _pin, article = self.pin_article(request, pin_slug)

        revision = get_object_or_404(ArticleRevision, id=revision_id, article=article)
        with transaction.atomic():
            article, _new_revision = restore_revision(scope_article=article, revision=revision, editor=request.user.profile)

        return Response(article_payload(article, request.user.profile))
