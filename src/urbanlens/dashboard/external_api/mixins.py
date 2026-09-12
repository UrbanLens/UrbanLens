"""Shared, cross-domain bases and mixins for the external API's endpoints.

Most of this package is credential-only: ``ExternalApiView`` accepts an ``ApiKey``/OAuth2 token and
nothing else, and an ordinary logged-in browser request cannot reach it (see the package docstring).
It is deliberately *not* the default: an endpoint should only opt in when the web client genuinely
needs the same URL, because every dual-auth endpoint is one more place where the credential boundary
depends on a per-view scope declaration rather than on the package boundary itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from oauth2_provider.contrib.rest_framework import OAuth2Authentication
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.errors import ErrorEnvelopeMixin
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope
from urbanlens.dashboard.external_api.throttling import ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle
from urbanlens.dashboard.models.reactions.model import Reaction
from urbanlens.dashboard.services.comments.comments import UnsupportedReactionEmojiError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Collection
    from typing import Any

    from django.db.models import Model
    from rest_framework.request import Request

    from urbanlens.dashboard.models.account.model import ApiKeyScope
    from urbanlens.dashboard.models.profile.model import Profile


class IsSessionAuthenticated(BasePermission):
    """Permits a request authenticated by a browser session rather than a credential.

    This exists because :class:`~urbanlens.dashboard.external_api.permissions.HasApiKeyScope` fails
    closed on ``auth is None`` - which is correct and must stay that way.
    Relaxing *it* to treat a missing credential as "no scope needed" would open every endpoint in this
    package to any logged-in session and destroy the boundary the package exists to draw; the narrow fix
    is a separate permission that only the deliberately dual-auth views OR in.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return True when this request carries a logged-in session and no credential.

        Args:
            request: The incoming DRF request.
            view: The view handling it (unused; required by the interface).

        Returns:
            True for a session-authenticated request, False when a credential authenticated it or nobody
            did.
        """
        user = getattr(request, "user", None)
        return request.auth is None and bool(user and user.is_authenticated)


class DualAuthJsonView(ErrorEnvelopeMixin, APIView):
    """A JSON endpoint reachable by browser cookies *or* an API key / OAuth2 token.

    Session-first answered "the cookie", which silently discarded the credential: ``request.auth``
    stayed None, ``IsSessionAuthenticated`` passed, and ``HasApiKeyScope`` was never consulted.
    That declaration only ever *restricts* credential callers: ``HasApiKeyScope`` fails closed when a
    method has no entry, so a credential can never reach a method whose requirements nobody declared,
    while a session caller is unaffected by it either way.
    """

    #: Credential first, session last - see the class docstring. Both credential authenticators return None when
    #: no ``Authorization`` header is present, so a cookie-only request still lands on
    #: ``SessionAuthentication``.
    authentication_classes = [ApiKeyAuthentication, OAuth2Authentication, SessionAuthentication]
    permission_classes = [IsAuthenticated & (HasApiKeyScope | IsSessionAuthenticated)]
    #: Same tiered per-credential caps the rest of the package uses. These are inert for session callers by
    #: construction - ``get_cache_key`` returns None without a credential, which is DRF's "don't throttle"
    #: signal - so adding them here cannot rate-limit the web UI.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle]
    #: JSON only.
    renderer_classes = [JSONRenderer]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {}

    @property
    def required_scopes(self) -> frozenset[ApiKeyScope]:
        """The scopes the current request's HTTP method requires of a credential caller."""
        return self.required_scopes_by_method.get(self.request.method or "", frozenset())


class _ReactionMixin:
    """Declarative PUT/DELETE handlers for one emoji reaction on one row.

    The only method a subclass writes is :meth:`resolve_reaction_target`, because "which row, and is
    this caller allowed near it" is genuinely per-domain.
    Why PUT/DELETE rather than a single toggling POST: the underlying services all *flip* state, which
    is unusable over a flaky mobile link - a retried POST silently undoes the reaction the first
    (successful but unacknowledged) attempt applied.
    """

    #: Which ``Reaction`` foreign key names this endpoint's target - one of ``"comment"``, ``"trip_comment"``,
    #: ``"direct_message"``, ``"group_message"``.
    reaction_target_field: ClassVar[str]

    #: The service function that flips one reaction, called as ``(profile, target, emoji)``. **Must** be wrapped
    #: in ``staticmethod()`` at the class level: a bare function assigned in a class body is a descriptor, so
    #: ``self.reaction_toggle(profile, target, emoji)`` would quietly pass the *view* as ``profile`` and the
    #: profile as the target.
    reaction_toggle: ClassVar[staticmethod[[Profile, Any, str], object]]

    #: The service function that renders the target's reaction summary, called as ``(target, profile)``. Same
    #: ``staticmethod()`` requirement as :attr:`reaction_toggle`.
    reaction_summarizer: ClassVar[staticmethod[[Any, Profile], Any]]

    #: The emoji vocabulary this endpoint accepts, or None to accept whatever the service accepts.
    reaction_allowed_emojis: ClassVar[Collection[str] | None] = None

    #: Top-level key wrapping the summary in the response body.
    reaction_response_key: ClassVar[str] = "reactions"

    #: Error body for an emoji rejected by :attr:`reaction_allowed_emojis`. Matches
    #: ``services.comments.comments.toggle_reaction``'s wording so a client sees one message whether the check
    #: fired here or in the service.
    reaction_invalid_emoji_message: ClassVar[str] = "That is not a supported reaction."

    def resolve_reaction_target(self, request: Request, **kwargs: Any) -> tuple[Model, Profile]:
        """Resolve the row being reacted to and the profile doing the reacting.

        Implementations **must** express authorization as part of the lookup (``get_object_or_404(Comment,
        id=..., wiki=wiki)``) rather than as a permission branch that returns 403, so that a row belonging
        to someone else is byte-identical to a row that does not exist.

        Args:
            request: The authenticated request. **kwargs: The URL keyword arguments, minus ``emoji`` which
            this mixin has already consumed.

        Returns:
            Tuple of (the reactable row, the requesting profile).

        Raises:
            Http404: No such row, or it is not one this caller may reach - deliberately indistinguishable.
            NotImplementedError: The subclass did not implement this.
        """
        raise NotImplementedError("A _ReactionMixin subclass must implement resolve_reaction_target().")

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: None})
    def put(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ensure the caller's reaction is present, idempotently.

        Args:
            request: The authenticated request. *args: Unused positional URL arguments. **kwargs: URL
            keyword arguments, including ``emoji``.

        Returns:
            The target's reaction summary under: attr:`reaction_response_key`, or a 400 for an unsupported
            emoji.
        """
        return self._react(request, want_present=True, **kwargs)

    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 404: None})
    def delete(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ensure the caller's reaction is absent, idempotently.

        Args:
            request: The authenticated request. *args: Unused positional URL arguments. **kwargs: URL
            keyword arguments, including ``emoji``.

        Returns:
            The target's reaction summary under: attr:`reaction_response_key`, or a 400 for an unsupported
            emoji.
        """
        return self._react(request, want_present=False, **kwargs)

    def _react(self, request: Request, *, want_present: bool, **kwargs: Any) -> Response:
        """Drive one reaction to the requested state and return the fresh summary.

        Args:
            request: The authenticated request.
            want_present: True for PUT (reaction should exist afterwards), False for DELETE. **kwargs: URL
            keyword arguments; ``emoji`` is consumed here and the...

        Returns:
            200 with ``{<reaction_response_key>: <summary>}``, or 400 when the emoji is not one this
            endpoint supports.

        Raises:
            KeyError: The route did not capture an ``emoji`` argument, which is a wiring bug in ``urls.py``
            rather than a client error - it is allowed to...
        """
        emoji = kwargs.pop("emoji")

        # Validated before the target is resolved, on purpose.
        allowed = self.reaction_allowed_emojis
        if allowed is not None and emoji not in allowed:
            return Response({"error": self.reaction_invalid_emoji_message}, status=400)

        target, profile = self.resolve_reaction_target(request, **kwargs)

        existing = Reaction.objects.existing(profile, emoji, **{self.reaction_target_field: target})
        if (existing is not None) != want_present:
            try:
                self.reaction_toggle(profile, target, emoji)
            except UnsupportedReactionEmojiError as exc:
                # The only exception a reaction service currently raises for a rejected emoji.
                logger.info("reaction toggle rejected: %s", exc)
                return Response({"error": self.reaction_invalid_emoji_message}, status=400)
            except ValueError as exc:
                # A future reaction service wired through this mixin that raises some other ValueError subclass
                # hasn't been audited for safe messaging yet, so its text stays server-side.
                logger.warning("Unhandled reaction-toggle ValueError: %s", exc, exc_info=True)
                return Response({"error": "Could not update this reaction."}, status=400)

        # The summary is read from the target's ``reactions`` relation, which may still be holding a prefetch
        # cache populated before the toggle; refreshing drops it so the response reflects the write that just
        # happened rather than the state the request arrived in.
        target.refresh_from_db()
        return Response({self.reaction_response_key: self.reaction_summarizer(target, profile)})
