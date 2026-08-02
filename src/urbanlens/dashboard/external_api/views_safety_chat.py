"""External-API endpoints for the *watcher* half of a safety check-in.

``views.py`` serves the explorer: their own check-ins, their edits, their
resolution. This module serves everyone else who is entitled to look - the
accepted partners - plus the one thing an invitee needs before they are anyone
at all, which is a way to answer the invitation.

Three gaps are closed here, and each was previously reachable only through the
website:

* **Chat had no REST endpoint.** The only non-session chat surface was the
  tokenized contact-portal WebSocket, which authenticates a *contact*, not the
  owner and not a partner. A mobile client could read a check-in and never see
  a word of the conversation happening on it.
* **An invited partner could not answer.** Invites could be sent through the
  API but only accepted in a browser, so the API could create a state it had no
  way to leave.
* **A partner could not mark the owner safe.** The single most useful thing a
  watcher does - "I found them, stand everyone down" - was web-only, while the
  escalation it stops runs on a five-minute beat.

**Addressing, and why it differs per endpoint.** Chat is reached by check-in
slug, because the caller may be the owner, for whom a slug is the natural
handle. Everything partner-facing is reached by **uuid only**: a check-in's slug
is unique per owner, not globally (``controllers.safety._get_checkin_as_partner``
has to swallow ``MultipleObjectsReturned`` for exactly this reason), so a slug
cannot safely name another person's check-in.

**404, never 403, everywhere.** A 403 on a check-in identifier confirms that
the identifier names a real, live check-in belonging to *someone* - i.e. that a
specific person is out somewhere right now - which is a disclosure even with no
payload attached. Every refusal on this surface, including "no such thing", is
one indistinguishable 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.mixins_safety import SafetyCheckinViewerScopedView
from urbanlens.dashboard.external_api.pagination import PaginatedListMixin
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_safety_chat import (
    PartneredCheckinDetailSerializer,
    PartneredCheckinListResponseSerializer,
    PartneredCheckinSummarySerializer,
    SafetyCheckinMessageCreateSerializer,
    SafetyCheckinMessageListResponseSerializer,
    SafetyCheckinMessageSerializer,
    SafetyPartnerInviteListResponseSerializer,
    SafetyPartnerRoleSerializer,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckin
from urbanlens.dashboard.services.visits.safety import (
    CheckinArchivedError,
    SafetyValidationError,
    accept_checkin_partner_invite,
    decline_checkin_partner_invite,
    get_partner_role,
    get_partnered_checkin,
    list_partnered_checkins,
    list_pending_partner_invites,
    mark_found_safe_by_partner,
    post_chat_message,
)

if TYPE_CHECKING:
    import uuid as uuid_module

    from rest_framework.request import Request


class SafetyCheckinMessagesView(SafetyCheckinViewerScopedView, PaginatedListMixin):
    """GET the shared chat on a check-in the caller may watch; POST a message to it.

    Built on :class:`~urbanlens.dashboard.external_api.mixins_safety.SafetyCheckinViewerScopedView`
    rather than the owner-scoped base, because the chat's whole point is that
    the owner and their accepted partners are talking to *each other*: an
    owner-scoped lookup would give the explorer a monologue and give partners
    nothing at all.

    Emergency **contacts** are deliberately not served here even when they have
    an account. A contact's access to a check-in is the tokenized portal, which
    only opens once a check-in escalates; routing them through this endpoint
    would hand a contact early access to a conversation they are not yet part
    of, on the strength of a credential the owner never granted them.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(responses={200: SafetyCheckinMessageListResponseSerializer, 404: ErrorSerializer})
    def get(self, request: Request, checkin_slug: str) -> Response:
        """Return one page of the check-in's chat, newest first.

        Args:
            request: The authenticated request.
            checkin_slug: Slug (or uuid) of the check-in.

        Returns:
            A paginated transcript, or a 404 if the caller may not watch this
            check-in - including when it does not exist.
        """
        checkin = self.get_viewable_checkin(request, checkin_slug)
        if checkin is None:
            return self.not_found()

        viewer = self.resolve_viewer(request)
        messages = (
            checkin.messages.select_related("sender_profile", "sender_contact")
            # Newest first, and *total*: `created` alone ties whenever two
            # messages land in the same instant - which is exactly what happens
            # when a system message is written alongside a user's, as
            # mark-safe and partner-accept both do - and an unstable sort
            # silently drops or repeats rows across page boundaries.
            .order_by("-created", "-pk")
        )
        # The viewer goes in the context rather than being compared client-side:
        # see SafetyCheckinMessageSerializer.is_mine for why display names are
        # not a safe identity comparison.
        return self.paginated_response(messages, SafetyCheckinMessageSerializer, request, context={"viewer": viewer})

    @extend_schema(request=SafetyCheckinMessageCreateSerializer, responses={201: SafetyCheckinMessageSerializer, 400: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, checkin_slug: str) -> Response:
        """Post a message to the check-in's chat.

        Goes through ``services.visits.safety.post_chat_message``, which is the same
        create-then-broadcast pair the no-JS web form uses. That is not a
        stylistic preference: a message merely *saved* is invisible in real time
        to everyone holding an open socket - the owner, the partners, and any
        contact sitting on the portal - until they happen to reload. A REST-sent
        message has to reach the live conversation exactly as a web-sent one
        does, and the only way to guarantee that is to not have a second send
        path at all.

        Args:
            request: The authenticated request, carrying ``body``.
            checkin_slug: Slug (or uuid) of the check-in.

        Returns:
            201 with the created message, 400 for a blank or over-long body,
            409 once the check-in has been archived, or 404 if the caller may
            not watch this check-in.
        """
        checkin = self.get_viewable_checkin(request, checkin_slug)
        if checkin is None:
            return self.not_found()

        serializer = SafetyCheckinMessageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        viewer = self.resolve_viewer(request)

        try:
            # contact=None: this endpoint authenticates an account, never a
            # portal token, so the sender is resolved to the caller's own
            # profile by resolve_message_sender.
            message = post_chat_message(checkin, user=request.user, contact=None, body=serializer.validated_data["body"])
        except CheckinArchivedError as exc:
            # A state conflict, not a malformed request: the body was fine, the
            # check-in's plaintext is simply already sealed into its encrypted
            # archive. A client must be able to tell this from a 400 so it can
            # retire the conversation instead of asking the user to retype.
            return Response({"error": exc.safe_message}, status=409)
        except SafetyValidationError as exc:
            # Anything the serializer's bounds did not already catch.
            return Response({"error": exc.safe_message}, status=400)

        return Response(SafetyCheckinMessageSerializer(message, context={"viewer": viewer}).data, status=201)


class SafetyPartnerScopedView(ExternalApiView):
    """Base for the endpoints a *partner* addresses, keyed by check-in uuid.

    Distinct from both check-in bases on purpose. The owner-scoped base answers
    "is this your check-in"; the viewer-scoped base answers "may you watch this
    check-in", which admits the owner as well. Neither is the question here.
    The partner surface asks "are you an accepted partner on someone *else's*
    check-in", and it must exclude the owner: an owner reaching a partner-only
    write would be marking themselves safe through an endpoint whose entire
    audit meaning is "somebody else confirmed they are alright".

    Subclasses resolve through ``services.visits.safety.get_partner_role`` (for the
    caller's own invitation row) or ``get_partnered_checkin`` (for a check-in
    they have accepted), and answer every miss with one of the two constant 404
    bodies below.
    """

    def resolve_caller(self, request: Request) -> Profile:
        """The profile the credential belongs to.

        Args:
            request: The authenticated request.

        Returns:
            The credential owner's profile, created on first use exactly as the
            session-backed controllers do - a user who has never loaded the web
            UI still has a valid account, and refusing them here would make the
            API's behaviour depend on whether they had ever visited the site.
        """
        profile, _created = Profile.objects.get_or_create(user=request.user)
        return profile

    def invite_not_found(self) -> Response:
        """The single 404 body every partner-invite lookup miss answers with.

        Constant across all three failure modes - no such check-in, a check-in
        the caller was never invited to, and an invitation that has already been
        declined - so the response cannot be used to distinguish them. In
        particular it must not become "you already declined this", which would
        confirm the check-in exists to anyone who guessed a uuid.

        Returns:
            A 404 response carrying the uniform error envelope.
        """
        return Response({"error": "No such invitation."}, status=404)

    def checkin_not_found(self) -> Response:
        """The single 404 body every partnered-check-in lookup miss answers with.

        Byte-identical to the owner- and viewer-scoped bases' 404 so that a
        client probing across surfaces cannot learn which one rejected it.

        Returns:
            A 404 response carrying the uniform error envelope.
        """
        return Response({"error": "No such check-in."}, status=404)

    def partnered_detail_response(self, checkin: SafetyCheckin, *, status: int = 200) -> Response:
        """Serialize *checkin* as the partner's full detail document.

        Re-fetches by pk with the annotations and prefetches the detail
        serializer needs, mirroring ``views.SafetyCheckinScopedView._detail_response``.
        Re-fetching rather than serializing the passed instance is what makes
        this correct after a write: a just-resolved check-in handed straight to
        the serializer would still carry the prefetch caches and the status the
        request arrived with.

        Args:
            checkin: The check-in to serialize - already authorized by the caller.
            status: HTTP status for the response.

        Returns:
            The detail response.
        """
        hydrated = (
            SafetyCheckin.objects.filter(pk=checkin.pk)
            .select_related("trip", "markup_map", "profile", "archive")
            .prefetch_related("contacts__contact_profile", "partners__profile", "partners__invited_by", "markup_maps")
            .annotate(contact_count=Count("contacts", distinct=True), partner_count=Count("partners", distinct=True))
            .first()
        )
        return Response(PartneredCheckinDetailSerializer(hydrated).data, status=status)


class SafetyPartnerInvitesView(SafetyPartnerScopedView, PaginatedListMixin):
    """GET the invitations awaiting the caller's answer.

    Not a convenience listing - it is the only way the accept and decline
    endpoints are reachable at all. Those are keyed by check-in uuid, and an
    invitee has no other way to learn one: they are not the owner, so the
    check-in appears in none of their own lists, and until they accept, the
    partner-checkin endpoints correctly refuse to resolve it. Without this
    endpoint an API-only client can be invited and then have no route to say
    yes.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
    }

    @extend_schema(responses={200: SafetyPartnerInviteListResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the caller's own pending partner invitations.

        Args:
            request: The authenticated request.

        Returns:
            A paginated list of the caller's INVITED partner rows, newest first.
            Only ever the caller's own: the queryset is scoped to their profile,
            so this cannot be used to enumerate anyone else's invitations.
        """
        return self.paginated_response(list_pending_partner_invites(self.resolve_caller(request)), SafetyPartnerRoleSerializer, request)


class SafetyPartnerInviteAcceptView(SafetyPartnerScopedView):
    """POST to accept a partner invitation, taking on the watching role."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(request=None, responses={200: SafetyPartnerRoleSerializer, 404: ErrorSerializer})
    def post(self, request: Request, checkin_uuid: uuid_module.UUID) -> Response:
        """Accept the caller's invitation to partner on this check-in.

        Idempotent by design, and 200 rather than an error on a repeat. A mobile
        client retries; if a re-accept answered 409, the extremely common case of
        "the first request succeeded but the response was lost" would surface to
        the user as a failure to take on a safety responsibility they have in
        fact already taken on. ``accept_checkin_partner_invite`` performs the
        flip as a conditional UPDATE, so a repeat is a genuine no-op rather than
        a second acceptance that re-notifies the owner.

        Args:
            request: The authenticated request.
            checkin_uuid: UUID of the check-in the invitation is for.

        Returns:
            200 with the caller's partner row, now ACCEPTED, or 404 if they hold
            no invitation for that check-in.
        """
        caller = self.resolve_caller(request)
        partner = get_partner_role(caller, str(checkin_uuid))
        if partner is None:
            return self.invite_not_found()

        accept_checkin_partner_invite(partner)

        # Re-read rather than serializing the in-memory row. The accept is a
        # conditional UPDATE that silently no-ops when the row no longer
        # matches, and the case that produces is real: the owner can remove the
        # invite between this request arriving and the update running. Without
        # the re-read that race answers 200 with a confident "accepted" payload
        # for a partnership that does not exist.
        refreshed = get_partner_role(caller, str(checkin_uuid))
        if refreshed is None:
            return self.invite_not_found()
        return Response(SafetyPartnerRoleSerializer(refreshed).data)


class SafetyPartnerInviteDeclineView(SafetyPartnerScopedView):
    """POST to decline a partner invitation, or to resign one already accepted."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(request=None, responses={204: None, 404: ErrorSerializer})
    def post(self, request: Request, checkin_uuid: uuid_module.UUID) -> Response:
        """Decline the caller's invitation, deleting their partner row.

        Repeating this is a 404, not another 204, and that is the honest answer
        rather than a wart: declining *deletes* the row, so on the second call
        there is genuinely no invitation to act on. Reporting success for a row
        that no longer exists would make the endpoint indistinguishable from one
        that had silently failed to delete anything.

        Note that no status filter guards this (see
        ``services.visits.safety.get_partner_role``), so an already-ACCEPTED partner can
        reach it to resign. That path revokes their live socket as well as their
        row - see ``services.visits.safety.decline_checkin_partner_invite``.

        Args:
            request: The authenticated request.
            checkin_uuid: UUID of the check-in the invitation is for.

        Returns:
            204 on success, or 404 if the caller holds no partner row for that
            check-in.
        """
        partner = get_partner_role(self.resolve_caller(request), str(checkin_uuid))
        if partner is None:
            return self.invite_not_found()
        decline_checkin_partner_invite(partner)
        return Response(status=204)


class SafetyPartnerCheckinsView(SafetyPartnerScopedView, PaginatedListMixin):
    """GET the check-ins the caller watches as an accepted partner."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
    }

    @extend_schema(responses={200: PartneredCheckinListResponseSerializer})
    def get(self, request: Request) -> Response:
        """Return one page of the check-ins the caller partners on.

        Args:
            request: The authenticated request.

        Returns:
            A paginated list of other people's check-ins where the caller holds
            an ACCEPTED partner row, newest deadline first. A merely-INVITED row
            does not appear - that is what the partner-invites listing is for.
        """
        return self.paginated_response(list_partnered_checkins(self.resolve_caller(request)), PartneredCheckinSummarySerializer, request)


class SafetyPartnerCheckinDetailView(SafetyPartnerScopedView):
    """GET the full document for one check-in the caller partners on."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
    }

    @extend_schema(responses={200: PartneredCheckinDetailSerializer, 404: ErrorSerializer})
    def get(self, request: Request, checkin_uuid: uuid_module.UUID) -> Response:
        """Return the partner's full detail document for one watched check-in.

        Args:
            request: The authenticated request.
            checkin_uuid: UUID of the check-in.

        Returns:
            200 with the detail document, or 404 when the caller is not an
            accepted partner on it - identical to the response for a uuid that
            names nothing.
        """
        checkin = get_partnered_checkin(self.resolve_caller(request), str(checkin_uuid))
        if checkin is None:
            return self.checkin_not_found()
        return self.partnered_detail_response(checkin)


class SafetyPartnerMarkSafeView(SafetyPartnerScopedView):
    """POST to conclude a watched check-in: the partner found the owner safe."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }

    @extend_schema(request=None, responses={200: PartneredCheckinDetailSerializer, 404: ErrorSerializer, 409: ErrorSerializer})
    def post(self, request: Request, checkin_uuid: uuid_module.UUID) -> Response:
        """Mark the check-in's owner safe, as an accepted partner.

        The write this whole surface exists for: it stops the escalation beat
        from emailing the owner's emergency contacts, and it does so from a
        phone, which is where a partner actually is when they find someone.

        Args:
            request: The authenticated request.
            checkin_uuid: UUID of the check-in.

        Returns:
            200 with the refreshed detail document, 409 if the check-in has
            already been resolved by someone else, or 404 if the caller is not
            an accepted partner on it.
        """
        caller = self.resolve_caller(request)
        checkin = get_partnered_checkin(caller, str(checkin_uuid))
        if checkin is None:
            return self.checkin_not_found()
        if checkin.is_resolved:
            # An up-front courtesy check only. The service resolves through a
            # conditional UPDATE, so a contact and a partner racing to report the
            # same person safe cannot both win regardless of what this sees.
            return Response({"error": "This check-in has already been resolved."}, status=409)

        mark_found_safe_by_partner(checkin, caller)
        return self.partnered_detail_response(checkin)
