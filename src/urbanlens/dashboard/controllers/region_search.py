"""Free-text place-name to polygonal boundary lookup, for filter regions."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, JsonResponse
from django.views import View

from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway
from urbanlens.dashboard.services.core.rate_limiter import EnvironmentRefusedError, RateLimitExceededError

_POLYGONAL_TYPES = ("Polygon", "MultiPolygon")

#: Seconds a client is told to wait when the budget refusal names no wait of its own.
_BUSY_RETRY_AFTER_SECONDS = 5


class RegionBoundarySearchView(LoginRequiredMixin, View):
    """Look up a place name's polygonal boundary for the Filters tab's region map.

    GET /region-search/?q=<free text> → JSON ``{"results": [{"display_name": str, "geojson": dict},

    ...]}``.
    Only candidates with a Polygon/MultiPolygon geometry are returned - point addresses and other
    non-area results are dropped, since they can't be drawn as an include/exclude region.
    """

    def get(self, request: HttpRequest) -> JsonResponse:
        """Look up polygonal boundaries matching a free-text place name.

        Args:
            request: The incoming GET request; ``q`` is the free-text place name to look up.

        Returns:
            A JSON response of the form ``{"results": [{"display_name": str, "geojson": dict}, ...]}``,
            limited to Polygon/MultiPolygon candidates; 429 with ``Retry-After`` while Nominatim's budget is spent,
            503 where this environment does not call it.
        """
        query = (request.GET.get("q") or "").strip()
        if not query:
            return JsonResponse({"results": []})

        gateway = NominatimGateway()
        try:
            raw_results = gateway.search(query, limit=5, polygon_geojson=1)
        except RateLimitExceededError as exc:
            response = JsonResponse({"error": "Place searches are momentarily rate limited - try again shortly."}, status=429)
            response["Retry-After"] = str(getattr(exc, "retry_after", _BUSY_RETRY_AFTER_SECONDS))
            return response
        except EnvironmentRefusedError:
            return JsonResponse({"error": "Place search isn't available in this environment."}, status=503)
        results = [{"display_name": result["display_name"], "geojson": result["geojson"]} for result in raw_results if isinstance(result.get("geojson"), dict) and result["geojson"].get("type") in _POLYGONAL_TYPES]
        return JsonResponse({"results": results})
