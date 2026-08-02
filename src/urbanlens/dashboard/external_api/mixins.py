"""Shared, cross-domain bases and mixins for the external API's endpoints.

Two unrelated concerns live here, both of them "more than one domain needs
exactly this behaviour and a second copy would drift":

**Dual authentication.** Most of this package is credential-only, and
:class:`DualAuthJsonView` is the narrow seam for the handful of endpoints the
site's own web client has to share with the mobile client. See its docstring
and the section below.

**Emoji reactions.** :class:`_ReactionMixin` owns the declarative PUT/DELETE
toggle that wiki comments, pin comments and group messages all present at
``.../reactions/{emoji}/``. Reactions are one model
(:class:`~urbanlens.dashboard.models.reactions.model.Reaction`) with a
polymorphic target, so the differences between those three endpoints are
entirely in *which row is being reacted to* - which makes three copies of the
idempotency, validation and 404 logic pure duplication.

---

Most of this package is credential-only: ``ExternalApiView`` accepts an
``ApiKey``/OAuth2 token and nothing else, and an ordinary logged-in browser
request cannot reach it (see the package docstring). A handful of endpoints
have to serve both audiences from one URL, though - the E2EE key-storage
views in ``controllers.e2ee`` are the site's own web client's key API *and*
the mobile client's, and duplicating them under a second prefix would mean
two implementations of the same delicate key-exchange contract drifting
apart.

:class:`DualAuthJsonView` is that seam. It is deliberately *not* the default:
an endpoint should only opt in when the web client genuinely needs the same
URL, because every dual-auth endpoint is one more place where the credential
boundary depends on a per-view scope declaration rather than on the package
boundary itself.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

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
from urbanlens.dashboard.services.comments.comments import CommentValidationError

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

    ``request.auth`` is the discriminator: every credential authenticator in
    this package stashes the ``ApiKey``/``AccessToken`` there, while
    ``SessionAuthentication`` sets a user and leaves ``auth`` as None. A
    session is therefore exactly the ``auth is None`` case.

    This exists because :class:`~urbanlens.dashboard.external_api.permissions.HasApiKeyScope`
    fails closed on ``auth is None`` - which is correct and must stay that
    way. Relaxing *it* to treat a missing credential as "no scope needed"
    would open every endpoint in this package to any logged-in session and
    destroy the boundary the package exists to draw; the narrow fix is a
    separate permission that only the deliberately dual-auth views OR in.

    Note:
        This grants no authority of its own beyond "is a logged-in session".
        The views composing it still apply their own ownership checks (a
        session can only ever reach its own key bundle), exactly as they did
        when they were ``LoginRequiredMixin`` Django views.
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return True when this request carries a logged-in session and no credential.

        Args:
            request: The incoming DRF request.
            view: The view handling it (unused; required by the interface).

        Returns:
            True for a session-authenticated request, False when a credential
            authenticated it or nobody did.
        """
        user = getattr(request, "user", None)
        return request.auth is None and bool(user and user.is_authenticated)


class DualAuthJsonView(ErrorEnvelopeMixin, APIView):
    """A JSON endpoint reachable by browser cookies *or* an API key / OAuth2 token.

    **Credentials are tried first, sessions only as a fallback.** DRF stops at
    the first authenticator that returns a result, so ordering here decides who
    a request that carries *both* a session cookie and an ``Authorization``
    header authenticates as. Session-first answered "the cookie", which silently
    discarded the credential: ``request.auth`` stayed None, ``IsSessionAuthenticated``
    passed, and ``HasApiKeyScope`` was never consulted. A WebView sharing the
    site's cookie jar - precisely the mobile client these routes were opened
    for - could therefore read the *session* account's wrapped key material
    while believing it was using its token, and a token missing ``messages:read``
    reached read endpoints on the strength of an unrelated cookie.

    Credential-first removes the ambiguity: a request that presents a credential
    is judged as that credential, scopes and all, and a malformed one is
    refused rather than quietly downgraded to ambient browser authority. The
    ordinary web client is unaffected - it sends no ``Authorization`` header, so
    both credential authenticators decline and ``SessionAuthentication`` handles
    it exactly as before, CSRF enforcement on unsafe methods included.

    The permission expresses "authenticated, and then either a credential
    carrying the right scopes or a plain session":
    ``IsAuthenticated & (HasApiKeyScope | IsSessionAuthenticated)``. The
    ``IsAuthenticated`` conjunct is not redundant - it produces the 401/403
    distinction for an anonymous caller before either branch is consulted.

    Subclasses declare scopes per HTTP method in ``required_scopes_by_method``.
    That declaration only ever *restricts* credential callers: ``HasApiKeyScope``
    fails closed when a method has no entry, so a credential can never reach a
    method whose requirements nobody declared, while a session caller is
    unaffected by it either way.

    ``ErrorEnvelopeMixin`` is listed before ``APIView`` so its
    ``get_exception_handler`` wins the MRO, giving these endpoints the same
    ``{"error": ...}`` envelope as the rest of the package. That applies to the
    session caller too, and deliberately so: the handlers here already answer
    every failure they raise themselves with ``{"error": ...}``, so before this
    the web client saw *that* shape for an application error but DRF's
    ``{"detail": ...}`` for a 403/404/405 from the same URL. One envelope is
    what lets its error path be written once. See ``external_api.errors``.
    """

    #: Credential first, session last - see the class docstring. Both credential
    #: authenticators return None when no ``Authorization`` header is present,
    #: so a cookie-only request still lands on ``SessionAuthentication``.
    authentication_classes = [ApiKeyAuthentication, OAuth2Authentication, SessionAuthentication]
    permission_classes = [IsAuthenticated & (HasApiKeyScope | IsSessionAuthenticated)]
    #: Same tiered per-credential caps the rest of the package uses. These are
    #: inert for session callers by construction - ``get_cache_key`` returns
    #: None without a credential, which is DRF's "don't throttle" signal - so
    #: adding them here cannot rate-limit the web UI.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle]
    #: JSON only. DRF's default renderer list would also offer the browsable
    #: HTML API, which would start serving an HTML page to anything sending
    #: ``Accept: text/html`` (a browser address bar, a link preview fetcher) -
    #: a change from the unconditional JSON these endpoints returned as plain
    #: Django views, and not a page that should exist for key material.
    renderer_classes = [JSONRenderer]
    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {}

    @property
    def required_scopes(self) -> frozenset[ApiKeyScope]:
        """The scopes the current request's HTTP method requires of a credential caller."""
        return self.required_scopes_by_method.get(self.request.method or "", frozenset())


class _ReactionMixin:
    """Declarative PUT/DELETE handlers for one emoji reaction on one row.

    Every reactable thing in this app - wiki comments, pin comments, trip
    comments, direct messages, group messages - stores its reactions in the
    single :class:`~urbanlens.dashboard.models.reactions.model.Reaction` table
    under a different nullable foreign key. The endpoints therefore differ only
    in *which row is addressed and who may address it*; the surrounding
    behaviour (idempotency, emoji validation, the response envelope, the
    no-oracle 404) is identical, and writing it out per domain is how one of
    them eventually ends up toggling on DELETE or answering 403 where its
    siblings answer 404.

    Subclasses **configure**, they do not reimplement. The only method a
    subclass writes is :meth:`resolve_reaction_target`, because "which row, and
    is this caller allowed near it" is genuinely per-domain. Everything else is
    class attributes.

    Why PUT/DELETE rather than a single toggling POST: the underlying services
    all *flip* state, which is unusable over a flaky mobile link - a retried
    POST silently undoes the reaction the first (successful but unacknowledged)
    attempt applied. PUT and DELETE are declarative, so this mixin reads the
    current state first and calls the service only when it actually differs.

    Example:
        ::

            class PinCommentReactionView(_ReactionMixin, ExternalApiView):
                reaction_target_field = "comment"
                reaction_allowed_emojis = ALLOWED_EMOJIS
                # staticmethod() is mandatory - see reaction_toggle below.
                reaction_toggle = staticmethod(toggle_reaction)
                reaction_summarizer = staticmethod(aggregate_reactions)

                def resolve_reaction_target(self, request, **kwargs):
                    pin = self.get_owned_pin_lite(request, kwargs["pin_slug"])
                    ...
    """

    #: Which ``Reaction`` foreign key names this endpoint's target - one of
    #: ``"comment"``, ``"trip_comment"``, ``"direct_message"``, ``"group_message"``.
    #: The authoritative list is ``models.reactions.queryset.REACTION_HOST_FIELDS``;
    #: ``Reaction.objects.existing`` raises ``ValueError`` for anything else, so a
    #: typo here fails loudly on the first request rather than silently matching
    #: every reaction that profile has ever left anywhere on the site. Used for the
    #: "does this reaction already exist" probe via ``Reaction.objects.existing``.
    #: The probe deliberately goes to the Reaction table rather than to the
    #: summary payload: summary shapes differ per domain (comments return
    #: ``{emoji: {...}}``, direct messages return a list of dicts), and a mixin
    #: that had to understand those shapes would need a branch per domain -
    #: which is the duplication this class exists to remove.
    reaction_target_field: ClassVar[str]

    #: The service function that flips one reaction, called as
    #: ``(profile, target, emoji)``. **Must** be wrapped in ``staticmethod()``
    #: at the class level: a bare function assigned in a class body is a
    #: descriptor, so ``self.reaction_toggle(profile, target, emoji)`` would
    #: quietly pass the *view* as ``profile`` and the profile as the target.
    #: The annotation is deliberately ``staticmethod[...]`` rather than
    #: ``Callable[...]`` so that mistake is a type error rather than a runtime
    #: surprise - mypy binds an instance-accessed ``Callable`` attribute as a
    #: method, which is exactly the behaviour being guarded against.
    reaction_toggle: ClassVar[staticmethod[[Profile, Any, str], object]]

    #: The service function that renders the target's reaction summary, called
    #: as ``(target, profile)``. Same ``staticmethod()`` requirement as
    #: :attr:`reaction_toggle`.
    reaction_summarizer: ClassVar[staticmethod[[Any, Profile], Any]]

    #: The emoji vocabulary this endpoint accepts, or None to accept whatever
    #: the service accepts. Setting it makes PUT and DELETE agree: without an
    #: up-front check, an unsupported emoji is only rejected on the branch that
    #: actually calls the service, so ``PUT`` of a junk emoji 400s while
    #: ``DELETE`` of the same junk emoji returns 200 - telling a buggy client
    #: its emoji vocabulary is fine when it is not.
    reaction_allowed_emojis: ClassVar[Collection[str] | None] = None

    #: Top-level key wrapping the summary in the response body.
    reaction_response_key: ClassVar[str] = "reactions"

    #: Error body for an emoji rejected by :attr:`reaction_allowed_emojis`.
    #: Matches ``services.comments.comments.toggle_reaction``'s wording so a client sees
    #: one message whether the check fired here or in the service.
    reaction_invalid_emoji_message: ClassVar[str] = "That is not a supported reaction."

    def resolve_reaction_target(self, request: Request, **kwargs: Any) -> tuple[Model, Profile]:
        """Resolve the row being reacted to and the profile doing the reacting.

        The one per-domain hook. Implementations **must** express authorization
        as part of the lookup (``get_object_or_404(Comment, id=..., wiki=wiki)``)
        rather than as a permission branch that returns 403, so that a row
        belonging to someone else is byte-identical to a row that does not
        exist. That is the same anti-enumeration discipline the wiki module's
        docstring lays out, and it is why this mixin has no hook for "is the
        caller allowed": a separate authorization step invites a 403 that
        confirms the id is real.

        Args:
            request: The authenticated request.
            **kwargs: The URL keyword arguments, minus ``emoji`` which this
                mixin has already consumed.

        Returns:
            Tuple of (the reactable row, the requesting profile).

        Raises:
            Http404: No such row, or it is not one this caller may reach -
                deliberately indistinguishable.
            NotImplementedError: The subclass did not implement this.
        """
        raise NotImplementedError("A _ReactionMixin subclass must implement resolve_reaction_target().")

    def put(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ensure the caller's reaction is present, idempotently.

        Args:
            request: The authenticated request.
            *args: Unused positional URL arguments.
            **kwargs: URL keyword arguments, including ``emoji``.

        Returns:
            The target's reaction summary under :attr:`reaction_response_key`,
            or a 400 for an unsupported emoji.
        """
        return self._react(request, want_present=True, **kwargs)

    def delete(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Ensure the caller's reaction is absent, idempotently.

        Args:
            request: The authenticated request.
            *args: Unused positional URL arguments.
            **kwargs: URL keyword arguments, including ``emoji``.

        Returns:
            The target's reaction summary under :attr:`reaction_response_key`,
            or a 400 for an unsupported emoji.
        """
        return self._react(request, want_present=False, **kwargs)

    def _react(self, request: Request, *, want_present: bool, **kwargs: Any) -> Response:
        """Drive one reaction to the requested state and return the fresh summary.

        Args:
            request: The authenticated request.
            want_present: True for PUT (reaction should exist afterwards),
                False for DELETE.
            **kwargs: URL keyword arguments; ``emoji`` is consumed here and the
                rest are handed to :meth:`resolve_reaction_target`.

        Returns:
            200 with ``{<reaction_response_key>: <summary>}``, or 400 when the
            emoji is not one this endpoint supports.

        Raises:
            KeyError: The route did not capture an ``emoji`` argument, which is
                a wiring bug in ``urls.py`` rather than a client error - it is
                allowed to surface loudly instead of being answered with a 400
                that would hide a broken route behind a plausible-looking
                client-side failure.
        """
        emoji = kwargs.pop("emoji")

        # Validated before the target is resolved, on purpose. A malformed
        # emoji is a request-format problem that reveals nothing about the
        # data, whereas resolving first would answer 404 for an id the caller
        # cannot see and 400 for one they can - turning an unsupported emoji
        # into a probe for whether a given comment/message id exists.
        allowed = self.reaction_allowed_emojis
        if allowed is not None and emoji not in allowed:
            return Response({"error": self.reaction_invalid_emoji_message}, status=400)

        target, profile = self.resolve_reaction_target(request, **kwargs)

        existing = Reaction.objects.existing(profile, emoji, **{self.reaction_target_field: target})
        if (existing is not None) != want_present:
            try:
                self.reaction_toggle(profile, target, emoji)
            except CommentValidationError as exc:
                # The only ValueError subclass a reaction service currently
                # raises for a rejected emoji; its message is a
                # developer-authored constant, so echoing it is safe. Anything
                # else - notably ``PermissionError`` from the direct-message
                # service - is left to propagate: a caller who got past
                # resolve_reaction_target and still is not permitted means the
                # subclass's lookup was not scoped tightly enough, and
                # swallowing that into a tidy 400 would hide the bug and
                # confirm the row exists at the same time.
                return Response({"error": exc.safe_message}, status=400)
            except ValueError as exc:
                # A future reaction service wired through this mixin that
                # raises some other ValueError subclass hasn't been audited
                # for safe messaging yet, so its text stays server-side.
                logger.warning("Unhandled reaction-toggle ValueError: %s", exc, exc_info=True)
                return Response({"error": "Could not update this reaction."}, status=400)

        # The summary is read from the target's ``reactions`` relation, which
        # may still be holding a prefetch cache populated before the toggle;
        # refreshing drops it so the response reflects the write that just
        # happened rather than the state the request arrived in.
        target.refresh_from_db()
        return Response({self.reaction_response_key: self.reaction_summarizer(target, profile)})
