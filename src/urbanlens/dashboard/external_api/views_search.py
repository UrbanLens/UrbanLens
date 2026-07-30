"""External-API view for global search across every domain the caller may read.

Backed end to end by ``services.global_search``: the same engine, the same
parser, the same ten providers the web search dialog runs. Nothing here
re-implements matching, ranking, or access scoping - a second implementation of
"which pins may this profile see" would drift from the first, and the direction
it drifts in is invisible to whoever tests it with their own data.

What *is* this layer's job is the scope gate, and it is the reason this module
exists as more than a thin wrapper:

    ``search:read`` is a floor, never a key to everything. Every provider is
    gated individually on the scope of the domain it reads, through
    :func:`~urbanlens.dashboard.external_api.permissions.filter_sources_by_grants`,
    and a section the credential cannot read is dropped from the provider chain
    before any query runs.

The case that must never regress is direct messages.
``DirectMessageSearchProvider`` returns plaintext excerpts of message bodies, and
``messages:read`` sits in ``OAUTH2_ONLY_SCOPES`` precisely so that a PAT-style
bearer key - the kind that ends up in CI configs, shell history and screenshots -
can never become a DM reader. A search endpoint that satisfied itself with
``search:read`` and then ran the default provider chain would route straight
around that boundary and hand DM content to any key at all. Hence the mapping
below, hence the per-provider filter, and hence
:class:`GlobalSearchScopeTests` in the test module.

Two smaller contracts worth stating, because both look like bugs to a reader who
does not know they are deliberate:

- **A too-short query is a 200, not a 400.** A search box issues a request per
  keystroke; refusing the first character would make every session begin with an
  error the user did not cause. ``q`` under two characters answers with
  ``total: 0``.
- **A denied section is never a 403.** The response degrades to the sections the
  credential *can* read and names the rest in ``omitted_types``. 403-ing the
  whole call would make search unusable for every narrowly-scoped integration,
  which is the failure mode ``filter_sources_by_grants`` was written against.
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

#: Every scope a credential must hold before the matching search provider is
#: allowed to run, keyed by ``RESULT_TYPES`` slug.
#:
#: Each entry lists ``SEARCH_READ`` *as well as* its domain scope, rather than
#: leaning on the view's own ``required_scopes`` having already been satisfied.
#: That is the contract ``filter_sources_by_grants`` documents: a section must
#: never be granted on the strength of a check that happened somewhere else,
#: because "somewhere else" is exactly what moves when a base class is
#: refactored.
#:
#: Three groupings are not one-to-one with the domain names and are worth
#: spelling out:
#:
#: - ``articles`` requires **both** ``pins:read`` and ``wiki:read``.
#:   ``ArticleSearchProvider`` returns the caller's own private pin articles
#:   *and* community wiki articles from a single queryset - it is one provider
#:   covering two domains, not a wiki-only one - so the section can only be
#:   granted when the credential holds every scope whose content it might
#:   surface. Gating on ``wiki:read`` alone would let a caller with no
#:   ``pins:read`` read private pin-article excerpts through search.
#: - ``maps`` requires ``pins:read``: a markup annotation is drawn on a pin, a
#:   wiki, or a standalone map, and its label routinely names the place it
#:   annotates.
#: - ``comments`` requires ``pins:read``, ``wiki:read`` **and** ``trips:read``
#:   together, for the same one-provider-many-domains reason as ``articles``:
#:   ``CommentSearchProvider`` quotes text from pin, wiki *and* trip comment
#:   threads in one pass. Requiring only ``pins:read`` would let that scope
#:   alone reach wiki and trip comment excerpts it was never granted for.
#:
#: Declared in ``RESULT_TYPES`` order so ``omitted_types`` comes back in a
#: stable, documented sequence rather than whatever a set happened to hash to.
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
    """GET: search pins, photos, wikis, articles, trips, visits, messages, maps, check-ins and comments at once.

    One request fans out across every domain the calling credential is scoped
    for and returns grouped, ranked previews - the API counterpart of the web
    search dialog, minus its search-history and hint endpoints (both are UI
    state, not data an integration needs).

    The payload is grouped rather than paginated. Ten sections of a handful of
    rows each is a different shape from a page of one kind of thing, and a
    ``?page=2`` over a heterogeneous ranked set would have no stable meaning:
    the sections are re-scored on every request. Callers wanting depth in one
    domain send ``types=<one type>`` with a larger ``limit``, which the engine
    already treats as a focused search.
    """

    required_scopes_by_method: ClassVar[dict[str, frozenset[ApiKeyScope]]] = {
        "GET": frozenset({ApiKeyScope.SEARCH_READ}),
    }
    #: The standard three plus a dedicated bucket. One search is up to ten
    #: providers' worth of database work, so charging it purely against the
    #: shared hourly read cap would let a burst of typing starve the sync
    #: traffic a mobile client actually depends on - see ``GlobalSearchThrottle``.
    throttle_classes = [ExternalApiBurstThrottle, ExternalApiReadThrottle, ExternalApiWriteThrottle, GlobalSearchThrottle]

    @extend_schema(parameters=[GlobalSearchQuerySerializer], responses={200: GlobalSearchResponseSerializer, 400: ErrorSerializer})
    def get(self, request: Request) -> Response:
        """Run one scoped global search and return its grouped results.

        Args:
            request: GET carrying ``q`` (the query, may be blank or absent),
                ``types`` (comma-separated ``RESULT_TYPES`` slugs, optional) and
                ``limit`` (results per section, optional).

        Returns:
            200 with the grouped payload - including for a query too short to
            search, which answers ``total: 0`` rather than an error. 400 only
            when a parameter is malformed (a ``limit`` outside 1-50, an
            over-long ``q``).
        """
        serializer = GlobalSearchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        params = serializer.validated_data

        query = params.get("q") or ""
        # Read from query_params rather than validated_data so an absent
        # parameter stays distinguishable from an explicitly blank one - the two
        # mean different things to parse_result_types.
        requested_types = parse_result_types(request.query_params.get("types"))

        # The scope gate. Applied by narrowing the provider *chain* rather than
        # by filtering the engine's output: a denied provider must never run its
        # queries at all, and a chain that never contained it cannot be put back
        # by some later branch forgetting to re-check.
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
            # Narrowed to what the caller actually asked about: telling a client
            # that requested only pins about its missing photos scope is noise it
            # cannot act on. With no explicit filter, every denied section is
            # named, which is what lets a client prompt for re-authorization.
            "omitted_types": [slug for slug in grants.omitted if requested_types is None or slug in requested_types],
            "groups": [
                {
                    "type": group.meta.slug,
                    "label": group.meta.label,
                    "icon": group.meta.icon,
                    # The SearchResult dataclasses go through untouched; the
                    # serializer's declared fields are what keeps `url` - a web
                    # path no API client can follow - out of the response. Build
                    # these dicts by hand and the next person to add a field to
                    # SearchResult leaks it here without noticing.
                    "results": group.results,
                }
                for group in response.groups
            ],
        }
        return Response(GlobalSearchResponseSerializer(payload).data)
