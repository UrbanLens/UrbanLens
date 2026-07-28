"""External-API reactions on a pin's own comment thread.

``views_wiki`` already exposes a pin's comments (list, create, delete) and a
wiki comment's reactions; the one gap this module closes is emoji reactions on
a *pin* comment, which the web UI has always had and the API did not.

Two decisions worth stating, because both look like they could go the other
way:

**Scope is ``pins:write``, not ``wiki:write``.** A pin's comment thread is the
owner's private annotation of their own pin, not shared community content -
which is why the neighbouring ``PinCommentsView``/``PinCommentDetailView`` use
the pin scopes too. Reacting under a community scope would let a key granted
only "edit community wikis" write to private per-pin data.

**The whole implementation is configuration.** The toggle semantics, emoji
vocabulary check, response envelope and 404 discipline live in
:class:`~urbanlens.dashboard.external_api.mixins._ReactionMixin`, shared with
the wiki-comment endpoint. The only thing genuinely per-domain is which row is
addressed and by whom, so that is the only thing written here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from django.http import Http404
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view

from urbanlens.dashboard.external_api.mixins import _ReactionMixin
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_pin_comments import CommentReactionsSerializer
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.external_api.views_pin_article import OwnedPinRequiredMixin
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.services.comments import ALLOWED_EMOJIS, aggregate_reactions, comment_is_visible, toggle_reaction

if TYPE_CHECKING:
    from rest_framework.request import Request

    from urbanlens.dashboard.models.profile.model import Profile


@extend_schema_view(
    put=extend_schema(responses={200: CommentReactionsSerializer, 400: ErrorSerializer, 404: ErrorSerializer}),
    delete=extend_schema(responses={200: CommentReactionsSerializer, 400: ErrorSerializer, 404: ErrorSerializer}),
)
class PinCommentReactionView(OwnedPinRequiredMixin, _ReactionMixin, ExternalApiView):
    """PUT to add an emoji reaction to a comment on the caller's pin; DELETE to remove it.

    Both handlers are inherited from ``_ReactionMixin`` and are declarative
    rather than toggling, so a retry over a flaky mobile link cannot undo the
    reaction the first (successful but unacknowledged) attempt applied. The
    schema annotations therefore live on ``extend_schema_view``: there is no
    local ``put``/``delete`` to decorate, and that is the point.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "PUT": frozenset({ApiKeyScope.PINS_WRITE}),
        "DELETE": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    reaction_target_field = "comment"
    reaction_allowed_emojis = ALLOWED_EMOJIS
    # staticmethod() is mandatory: a bare function assigned in a class body is a
    # descriptor, so the view itself would be passed as the first argument.
    reaction_toggle = staticmethod(toggle_reaction)
    reaction_summarizer = staticmethod(aggregate_reactions)

    def resolve_reaction_target(self, request: Request, **kwargs: Any) -> tuple[Comment, Profile]:
        """Resolve the pin comment being reacted to, or 404.

        Every gate is expressed as a lookup rather than a permission branch:
        ``get_owned_pin_lite`` filters on the credential owner, ``pin=pin``
        scopes the comment id to that pin, and :func:`comment_is_visible`
        applies the same per-comment gates the thread listing does. A comment on
        someone else's pin, a comment id from a different pin of the caller's
        own, and a comment this caller was never shown are therefore all
        indistinguishable from one that was never created, which is what stops
        the sequential comment ids from being enumerable.

        Owning the pin is not the same as being allowed to read every comment
        on it: others comment on shared pins, and their comment-visibility
        setting, a pending malware scan, or an ``@loc`` mention the caller has
        not pinned each drop a comment from the thread this caller sees. Scoped
        by pin alone, reacting by id reached all three.

        Args:
            request: The authenticated request.
            **kwargs: URL keyword arguments; ``pin_slug`` and ``comment_id``
                are read here.

        Returns:
            Tuple of (the comment, the requesting profile).

        Raises:
            Http404: No such pin for this caller, that comment is not on it, or
                it is not visible to them.
        """
        pin = self.get_owned_pin_or_404(request, kwargs["pin_slug"])
        comment = get_object_or_404(Comment.objects.select_related("profile"), id=kwargs["comment_id"], pin=pin)
        profile = request.user.profile
        if not comment_is_visible(comment, profile):
            raise Http404
        return comment, profile
