"""External-API endpoint for answering a pin someone shared with you.

A friend can share a pin into a direct message, a group chat, or as a bare
notification. Every one of those already reaches the mobile client - the
message and notification endpoints deliver the share - but until now the
recipient had no way to act on it without opening the website. This closes
that: one POST accepts or rejects, and accepting materialises the pin (and any
bundled child pins) on the recipient's own map.

**Scope is ``pins:write``, not ``messages:*``.** The share is *about* a pin -
responding creates one - and the same ``PinShare`` row is delivered by bare
notification as well as by message, so scoping this to the messaging domain
would lock a PAT holder out of exactly the shares that never went through a
conversation. ``messages:*`` is additionally OAuth2-only, so that choice would
also have made this unreachable for every personal access token.

**The scoped lookup is the entire anti-enumeration story, and it has to be.**
``PinShare`` primary keys are sequential integers, so ``pk=share_id`` alone
would let a caller walk other people's inboxes and - far worse than reading -
*accept or reject* shares addressed to strangers, mutating rows they were never
shown. ``to_profile=`` in the same ``get_object_or_404`` makes an id belonging
to someone else indistinguishable from one that does not exist, and it must
stay part of the lookup rather than becoming a check that answers 403.

**Do not add ``record_share_exposure`` here.** The instinct is right in general
and wrong in this specific place; ``services.pin_sharing``'s module docstring
explains why at length, and there is a test asserting the ``LocationExposure``
row count is unchanged across an accept.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_pin_shares import PinShareRespondResultSerializer, PinShareRespondSerializer
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.pin_share import PinShare, PinShareStatus
from urbanlens.dashboard.services.pin_sharing import apply_pin_share_response

if TYPE_CHECKING:
    from rest_framework.request import Request


class PinShareRespondView(ExternalApiView):
    """POST to accept or reject a pin someone shared with the caller."""

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "POST": frozenset({ApiKeyScope.PINS_WRITE}),
    }

    @extend_schema(request=PinShareRespondSerializer, responses={200: PinShareRespondResultSerializer, 400: ErrorSerializer, 404: ErrorSerializer})
    def post(self, request: Request, share_id: int) -> Response:
        """Apply the caller's decision to a share addressed to them.

        Args:
            request: The authenticated request carrying ``action``.
            share_id: The share being answered.

        Returns:
            200 with the share's resulting status, the recipient-side pin slug
            (accept only), and a human-readable summary. 400 when the share was
            already handled - re-answering must not silently re-run acceptance
            and create a second pin. 404 when no such share exists *or* it is
            addressed to somebody else.
        """
        serializer = PinShareRespondSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # to_profile= belongs in the lookup, never in a follow-up check: share
        # ids are sequential, so a 403 (or any distinguishable answer) for
        # someone else's id would turn this into an inbox scanner - and the
        # rows it addresses are writable.
        share = get_object_or_404(
            PinShare.objects.select_related("pin__location", "location"),
            pk=share_id,
            to_profile=request.user.profile,
        )

        if share.status != PinShareStatus.PENDING:
            # Not idempotent-and-silent: a retried accept on an already-accepted
            # share must not run the acceptance path again, and telling the
            # client "already handled" is safe here because they were provably
            # shown this share (the lookup above proved it is addressed to them).
            return Response({"error": "This shared pin has already been handled."}, status=400)

        target_pin, message = apply_pin_share_response(share, serializer.validated_data["action"])

        return Response(
            {
                "status": share.status,
                # Falls back to the uuid because a Pin's slug is derived from
                # its name and a share can produce an unnamed pin (a
                # location-only share detected in a message); the client needs
                # an addressable identifier either way, and every pin-scoped
                # route in this API accepts both.
                "pin_slug": (target_pin.slug or str(target_pin.uuid)) if target_pin is not None else None,
                "detail": message,
            },
        )
