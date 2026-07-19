"""Per-API-key rate limiting for the external API.

Keyed by the API key itself, not the underlying user or IP: a user with
several keys (e.g. one per connected app) shouldn't have one misbehaving app
burn through a budget shared with their other keys, which is what DRF's
built-in ``UserRateThrottle``/``AnonRateThrottle`` (used by the internal API,
see ``REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]``) would do instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework.throttling import SimpleRateThrottle

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView


class ApiKeyRateThrottle(SimpleRateThrottle):
    """Rate-limits external API requests per ``ApiKey``, using the ``external_api_key`` scope rate."""

    scope = "external_api_key"

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        """Build the throttle cache key from the authenticated ``ApiKey``, or opt out entirely.

        Returning ``None`` disables throttling for the request (DRF's
        convention) - correct here since a request with no resolved API key
        will already be rejected by authentication/permissions before this
        would matter.
        """
        api_key = getattr(request, "auth", None)
        if api_key is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": api_key.pk}
