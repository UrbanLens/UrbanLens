"""External-facing REST views: extremely limited, API-key-gated access.

Every view here is authenticated by ``ApiKeyAuthentication`` and gated by
``HasApiKeyScope`` - neither the internal session-authenticated REST surface
nor an ordinary logged-in browser request can reach these. See the package
docstring in ``__init__.py`` for the boundary rationale.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rest_framework.response import Response
from rest_framework.views import APIView

from urbanlens.dashboard.external_api.authentication import ApiKeyAuthentication
from urbanlens.dashboard.external_api.permissions import HasApiKeyScope
from urbanlens.dashboard.external_api.serializers import PinCreateSerializer, WhoAmISerializer
from urbanlens.dashboard.external_api.throttling import ApiKeyRateThrottle
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.pin_creation import PinCreationError, PinCreationForbiddenError, create_pin_for_profile

if TYPE_CHECKING:
    from rest_framework.request import Request

logger = logging.getLogger(__name__)


class WhoAmIView(APIView):
    """GET: the calling API key's owner - just their uuid, nothing else.

    This is the only read access an external application has: no pins,
    lists, trips, or any other private data, per the ``profile:read`` scope's
    definition.
    """

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasApiKeyScope]
    throttle_classes = [ApiKeyRateThrottle]
    required_scopes = frozenset({ApiKeyScope.PROFILE_READ})

    def get(self, request: Request) -> Response:
        """Return the authenticated key owner's profile uuid."""
        profile = request.user.profile
        return Response(WhoAmISerializer(profile).data)


class PinCreateView(APIView):
    """POST: create a pin for the calling API key's owner.

    Goes through the exact same ``services.pin_creation.create_pin_for_profile``
    call as the map UI's "Add pin" form - the same sanitization, geocoding
    gate, and background enrichment apply regardless of which caller created
    the pin. All submitted data is untrusted and is validated by
    ``PinCreateSerializer`` before it ever reaches that shared function.
    """

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [HasApiKeyScope]
    throttle_classes = [ApiKeyRateThrottle]
    required_scopes = frozenset({ApiKeyScope.PINS_WRITE})

    def post(self, request: Request) -> Response:
        """Validate the payload and create a pin owned by the key's user."""
        serializer = PinCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            result = create_pin_for_profile(
                request.user.profile,
                name=data.get("name"),
                latitude=data.get("latitude"),
                longitude=data.get("longitude"),
                address=data.get("address"),
                icon=data.get("icon"),
                color=data.get("color"),
            )
        except PinCreationForbiddenError as exc:
            return Response({"error": str(exc)}, status=403)
        except PinCreationError as exc:
            return Response({"error": str(exc)}, status=400)

        pin = result.pin
        return Response(
            {
                "uuid": str(pin.uuid),
                "slug": pin.slug,
                "name": pin.effective_name,
                # True when the coordinates also match another existing Location -
                # the pin was still created, but callers may want to flag this for
                # manual review rather than silently trusting the auto-resolved place.
                "ambiguous_location": len(result.all_locations) > 1,
            },
            status=201,
        )
