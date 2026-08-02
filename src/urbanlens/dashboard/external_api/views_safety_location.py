"""External-API endpoint for a safety check-in's live location.

**Deliberately its own endpoint, excluded from the check-in summary/detail
serializers** - see ``SafetyCheckinDetailSerializer``'s docstring. Live
location is a continuously-updating precise position, a materially different
privacy proposition from the rest of a check-in's document; folding it into
the general read would mean any ``safety:read`` credential that only ever
wanted to sync check-in metadata quietly started accumulating a movement
trail too.

Reuses ``safety:read``/``safety:write`` rather than minting a dedicated
scope - the containment that matters here is structural (this endpoint, and
only this endpoint, ever touches the ``live_*`` columns), not an extra scope
gate a client would have to separately understand and grant.

**Residual risk, stated plainly:** a leaked credential holding ``safety:read``
becomes a live tracker for every check-in its holder partners on, for as long
as that check-in stays active and sharing stays on. That is the same exposure
the WebSocket location group already carries for a connected browser tab; this
endpoint does not create a new capability, only a polling-friendly REST path
to the same data.

Visibility mirrors the chat consumer's model exactly
(``consumers.SafetyCheckinChatConsumer``, ``services.visits.safety.is_owner_or_accepted_partner``):
GET is open to the owner or a currently-ACCEPTED partner, via
:class:`~urbanlens.dashboard.external_api.mixins_safety.SafetyCheckinViewerScopedView`.
A declined or removed partner loses read access exactly like they lose the
chat's WebSocket group - both ask the same predicate. PATCH is narrower: only
the owner may report or toggle their own position, checked explicitly below
rather than by switching to the owner-only base, so both verbs share one 404
body and a partner probing PATCH cannot tell "not your check-in" from "you're
not even a partner on it".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.mixins_safety import SafetyCheckinViewerScopedView
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_safety_location import SafetyCheckinLocationSerializer, SafetyCheckinLocationUpdateSerializer
from urbanlens.dashboard.external_api.throttling import ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, SafetyLocationThrottle
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.visits.safety import SafetyValidationError, set_live_location_sharing, update_live_location

if TYPE_CHECKING:
    from rest_framework.request import Request


class SafetyCheckinLocationView(SafetyCheckinViewerScopedView):
    """GET the owner's current shared position; PATCH to report a fix or toggle sharing (owner only)."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SAFETY_READ}),
        "PATCH": frozenset({ApiKeyScope.SAFETY_WRITE}),
    }
    #: SafetyLocationThrottle is write-tier (see throttling.request_tier), so it
    #: only ever counts PATCH here - GET stays under the ordinary read cap, the
    #: right budget for a partner polling for updates.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, SafetyLocationThrottle]

    @extend_schema(responses={200: SafetyCheckinLocationSerializer, 404: ErrorSerializer})
    def get(self, request: Request, checkin_slug: str) -> Response:
        """Return the check-in owner's current shared position.

        Args:
            request: The authenticated request.
            checkin_slug: Slug (or uuid) of the check-in.

        Returns:
            200 with the current position (null fields when sharing is off),
            or 404 when the caller may not watch this check-in - including
            when it does not exist.
        """
        checkin = self.get_viewable_checkin(request, checkin_slug)
        if checkin is None:
            return self.not_found()
        return Response(SafetyCheckinLocationSerializer(checkin).data)

    @extend_schema(request=SafetyCheckinLocationUpdateSerializer, responses={200: SafetyCheckinLocationSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def patch(self, request: Request, checkin_slug: str) -> Response:
        """Report a new fix and/or toggle sharing, as the check-in's owner.

        A partner resolves the same check-in through :meth:`get_viewable_checkin`
        but is refused here exactly like a nonexistent one - a 403 would
        confirm the slug names a real check-in belonging to someone, which
        this surface never does for any reason.

        Args:
            request: The authenticated request.
            checkin_slug: Slug (or uuid) of the check-in.

        Returns:
            200 with the refreshed position, 400 when a position was
            submitted while sharing is off (and not being turned on in the
            same request) or the check-in has already resolved, or 404 when
            the caller isn't this check-in's owner - including when it
            doesn't exist or the caller only partners on it.
        """
        checkin = self.get_viewable_checkin(request, checkin_slug)
        if checkin is None:
            return self.not_found()
        if checkin.profile_id != self.resolve_viewer(request).pk:
            return self.not_found()

        serializer = SafetyCheckinLocationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if "sharing_enabled" in data:
            set_live_location_sharing(checkin, enabled=data["sharing_enabled"])

        if "latitude" in data:
            try:
                update_live_location(checkin, latitude=data["latitude"], longitude=data["longitude"], accuracy=data.get("accuracy"))
            except SafetyValidationError as exc:
                return Response({"error": exc.safe_message}, status=400)

        return Response(SafetyCheckinLocationSerializer(checkin).data)
