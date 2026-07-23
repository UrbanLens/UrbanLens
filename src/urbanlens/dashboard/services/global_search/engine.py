"""Global-search orchestration: parse, fan out to providers, group results."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.global_search.parser import ParsedQuery, parse_query
from urbanlens.dashboard.services.global_search.providers import SearchProvider, default_providers
from urbanlens.dashboard.services.global_search.results import RESULT_TYPES, ResultTypeMeta, SearchResult

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: Results per section on a mixed (all-types) search.
DEFAULT_SECTION_LIMIT = 6
#: Results per section when the query names specific types ("pins in ...").
FOCUSED_SECTION_LIMIT = 25
#: Minimum query length before searching at all.
MIN_QUERY_LENGTH = 2


@dataclass(slots=True)
class SearchGroup:
    """One rendered section: a result type and its hits."""

    meta: ResultTypeMeta
    results: list[SearchResult]


@dataclass(slots=True)
class SearchResponse:
    """Everything the results partial needs to render one search.

    Attributes:
        parsed: The structured interpretation of the query.
        groups: Non-empty result sections, in RESULT_TYPES order.
        errors: Human-readable notices for sections that failed; searching
            stays useful even when one provider errors.
        total: Total result count across groups.
        used_fallback: True when the structured interpretation found nothing
            and the plain-text retry produced these results instead.
    """

    parsed: ParsedQuery
    groups: list[SearchGroup] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    total: int = 0
    used_fallback: bool = False


class GlobalSearchEngine:
    """Runs every applicable provider for a query and groups the results.

    Args:
        providers: Provider chain override, mainly for tests; defaults to
            :func:`default_providers`.
    """

    def __init__(self, providers: list[SearchProvider] | None = None) -> None:
        self.providers = providers if providers is not None else default_providers()

    def search(self, profile: Profile, raw_query: str) -> SearchResponse:
        """Search everything the profile has access to.

        A failing provider contributes an error notice instead of failing the
        whole search. When a structured interpretation (parsed place/date/type)
        matches nothing, the query is retried as plain text so a literal name
        like "stairs in the mill" still finds its pin.

        Args:
            profile: The requesting user's profile.
            raw_query: The query exactly as typed.

        Returns:
            The grouped, ordered results.
        """
        parsed = parse_query(raw_query)
        if len(" ".join(raw_query.split())) < MIN_QUERY_LENGTH or parsed.is_empty:
            return SearchResponse(parsed=parsed)

        if parsed.near_me:
            # The parser has no profile/DB access, so "near me" is resolved to
            # coordinates here, once we know who is searching.
            point = profile.best_known_point()
            if point is not None:
                parsed.near_lat, parsed.near_lng = point

        response = self._run(profile, parsed)
        if response.total == 0 and parsed.has_structure and parsed.raw.strip():
            fallback = ParsedQuery(raw=parsed.raw)
            fallback.terms = [token.lower() for token in parsed.raw.split()]
            fallback.text = " ".join(fallback.terms)
            fallback_response = self._run(profile, fallback)
            if fallback_response.total > 0:
                fallback_response.parsed = parsed
                fallback_response.used_fallback = True
                return fallback_response
        return response

    def _run(self, profile: Profile, parsed: ParsedQuery) -> SearchResponse:
        """Fan one parsed query out to the applicable providers."""
        response = SearchResponse(parsed=parsed)
        active = [provider for provider in self.providers if not parsed.types or provider.slug in parsed.types]
        limit = FOCUSED_SECTION_LIMIT if len(parsed.types) == 1 else DEFAULT_SECTION_LIMIT

        for provider in active:
            meta = RESULT_TYPES.get(provider.slug)
            if meta is None:
                continue
            try:
                results = provider.search(profile, parsed, limit)
            except Exception:
                logger.exception("Global search provider '%s' failed for query %r", provider.slug, parsed.raw)
                response.errors.append(f"{meta.label} could not be searched right now.")
                continue
            if results:
                results.sort(key=lambda result: result.score, reverse=True)
                response.groups.append(SearchGroup(meta=meta, results=results))
                response.total += len(results)
        return response
