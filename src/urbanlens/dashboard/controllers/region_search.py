"""Free-text place-name to polygonal boundary lookup, for filter regions."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views import View

from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

_POLYGONAL_TYPES = ("Polygon", "MultiPolygon")


class RegionBoundarySearchView(LoginRequiredMixin, View):
    """Look up a place name's polygonal boundary for the Filters tab's region map.

    GET /region-search/?q=<free text> → JSON ``{"results": [{"display_name": str, "geojson": dict}, ...]}``.

    Only candidates with a Polygon/MultiPolygon geometry are returned - point
    addresses and other non-area results are dropped, since they can't be
    drawn as an include/exclude region.
    """

    def get(self, request):
        query = (request.GET.get("q") or "").strip()
        if not query:
            return JsonResponse({"results": []})

        gateway = NominatimGateway()
        raw_results = gateway.search(query, limit=5, polygon_geojson=1)
        results = [
            {"display_name": result["display_name"], "geojson": result["geojson"]}
            for result in raw_results
            if isinstance(result.get("geojson"), dict) and result["geojson"].get("type") in _POLYGONAL_TYPES
        ]
        return JsonResponse({"results": results})
