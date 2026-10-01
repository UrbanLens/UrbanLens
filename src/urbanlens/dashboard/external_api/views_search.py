"""External-API view for global search across every domain the caller may read.

Nothing here re-implements matching, ranking, or access scoping - a second implementation of "which
pins may this profile see" would drift from the first, and the direction it drifts in is invisible
to whoever tests it with their own data.
The case that must never regress is direct messages.

- **A too-short query is a 200, not a 400.** A search box issues a request per keystroke; refusing
  the first character would make every session begin with an e...
- **A denied section is never a 403.** The response degrades to the sections the credential *can*
  read and names the rest in ``omitted_types``. 403-ing the who...
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response

from urbanlens.dashboard.external_api.permissions import filter_sources_by_grants
from urbanlens.dashboard.external_api.serializers import ErrorSerializer
from urbanlens.dashboard.external_api.serializers_search import (
    GlobalSearchQuerySerializer,
    GlobalSearchResponseSerializer,
    parse_result_types,
)
from urbanlens.dashboard.external_api.throttling import (
    ExternalApiBurstThrottle,
    ExternalApiReadThrottle,
    ExternalApiWriteThrottle,
    GlobalSearchThrottle,
)
from urbanlens.dashboard.external_api.views import ExternalApiView
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.global_search.providers import default_providers

if TYPE_CHECKING:
    from rest_framework.request import Request

logger = logging.getLogger(__name__)

#: Every scope a credential must hold before the matching search provider is allowed to run, keyed by
#: ``RESULT_TYPES`` slug. Requiring only ``pins:read`` would let that scope alone reach wiki and trip comment
#: excerpts it was never granted for.
SEARCH_SECTION_SCOPES: dict[str, frozenset[ApiKeyScope]] = {
    "pins": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.PINS_READ}),
    "photos": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.PHOTOS_READ}),
    "wikis": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.WIKI_READ}),
    "articles": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.PINS_READ, ApiKeyScope.WIKI_READ}),
    "trips": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.TRIPS_READ}),
    "visits": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.VISITS_READ}),
    "messages": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.MESSAGES_READ}),
    "maps": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.PINS_READ}),
    "safety": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.SAFETY_READ}),
    "comments": frozenset({ApiKeyScope.SEARCH_READ, ApiKeyScope.PINS_READ, ApiKeyScope.WIKI_READ, ApiKeyScope.TRIPS_READ}),
}


class GlobalSearchView(ExternalApiView):
    """GET: search pins, photos, wikis, articles, trips, visits, messages, maps, check-ins and comments at
    once.

    The payload is grouped rather than paginated.
    Ten sections of a handful of rows each is a different shape from a page of one kind of thing, and a
    ``?page=2`` over a heterogeneous ranked set would have no stable meaning: the sections are re-scored
    on every request.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SEARCH_READ}),
    }
    #: The standard three plus a dedicated bucket. One search is up to ten providers' worth of database work, so
    #: charging it purely against the shared hourly read cap would let a burst of typing starve the sync traffic
    #: a mobile client actually depends on - see ``GlobalSearchThrottle``.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, GlobalSearchThrottle]

    @extend_schema(parameters=[GlobalSearchQuerySerializer], responses={200: GlobalSearchResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Run one scoped global search and return its grouped results.

        Args:
            request: GET carrying ``q`` (the query, may be blank or absent), ``types`` (comma-separated
            ``RESULT_TYPES`` slugs, optional) and ``limit``...

        Returns:
            200 with the grouped payload - including for a query too short to search, which answers ``total:
            0`` rather than an error.
        """
        serializer = GlobalSearchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        query = params.get("q") or ""
        # Read from query_params rather than validated_data so an absent parameter stays distinguishable from an
        # explicitly blank one - the two mean different things to parse_result_types.
        requested_types = parse_result_types(request.query_params.get("types"))

        # The scope gate.
        grants = filter_sources_by_grants(request.auth, SEARCH_SECTION_SCOPES)
        providers = [provider for provider in default_providers() if provider.slug in grants]

        response = GlobalSearchEngine(providers).search(
            request.user.profile,
            query,
            types=requested_types,
            limit=params.get("limit"),
        )

        payload: dict[str, Any] = {
            "query": query,
            "total": response.total,
            "used_fallback": response.used_fallback,
            "filter_chips": response.parsed.describe_filters(),
            "errors": response.errors,
            # Narrowed to what the caller actually asked about: telling a client that requested only pins about
            # its missing photos scope is noise it cannot act on.
            "omitted_types": [slug for slug in grants.omitted if requested_types is None or slug in requested_types],
            "groups": [
                {
                    "type": group.meta.slug,
                    "label": group.meta.label,
                    "icon": group.meta.icon,
                    # The SearchResult dataclasses go through untouched; the serializer's declared fields are
                    # what keeps `url` - a web path no API client can follow - out of the response.
                    "results": group.results,
                }
                for group in response.groups
            ],
        }
        return Response(GlobalSearchResponseSerializer(payload).data)
