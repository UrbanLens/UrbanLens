"""Global-search orchestration: parse, fan out to providers, group results."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.global_search.parser import ParsedQuery, extract_fallback_terms, parse_query
from urbanlens.dashboard.services.global_search.providers import SearchProvider, default_providers
from urbanlens.dashboard.services.global_search.results import RESULT_TYPES, ResultTypeMeta, SearchResult

if TYPE_CHECKING:
    from collections.abc import Iterable

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

    The provider chain is the *only* place access to a whole result type is
    decided. Callers that may not read everything - notably the external API,
    where a credential's scopes decide which domains it can touch at all - are
    expected to pass a narrowed chain rather than to filter the response
    afterwards. Post-filtering would still have run the excluded providers'
    queries, and one forgotten branch would put the excluded section straight
    back into the payload.

    Args:
        providers: Provider chain override; defaults to
            :func:`default_providers`. An empty list is honoured as "search
            nothing", not corrected to the default - see above.
    """

    def __init__(self, providers: list[SearchProvider] | None = None) -> None:
        self.providers = providers if providers is not None else default_providers()

    def search(self, profile: Profile, raw_query: str, *, types: Iterable[str] | None = None, limit: int | None = None) -> SearchResponse:
        """Search everything the profile has access to.

        A failing provider contributes an error notice instead of failing the
        whole search. When a structured interpretation (parsed place/date/type)
        matches nothing, the query is retried as plain text so a literal name
        like "stairs in the mill" still finds its pin.

        Args:
            profile: The requesting user's profile.
            raw_query: The query exactly as typed.
            types: ``RESULT_TYPES`` slugs to restrict the search to, overriding
                anything the parser inferred from the query text. Meant for a
                caller with a real type picker (the API's ``types=`` parameter),
                where the restriction is the user's stated intent rather than a
                guess - which is why, unlike the parser's inference, it is *not*
                dropped by the plain-text fallback below. None leaves the
                parser's interpretation in charge; an empty collection is a
                deliberate "no types", and searches nothing.
            limit: Results per section, overriding the query-shape-derived
                default. Bounds checking belongs to the caller.

        Returns:
            The grouped, ordered results.
        """
        parsed = parse_query(raw_query)
        # Kept separate from `parsed.types` on purpose. `parsed.types` uses the
        # parser's convention where *empty means every type*, which is right for
        # an inference ("no type word was recognized") and catastrophically wrong
        # for a caller's explicit choice: `?types=messages` from a credential
        # with no messages scope resolves to an empty restriction, and folding
        # that into `parsed.types` would silently reopen every other section.
        # `restrict` distinguishes "no restriction" (None) from "restricted to
        # nothing" (empty), so the empty case searches nothing.
        restrict = frozenset(types) if types is not None else None
        if restrict is not None:
            # Mirrored onto the parsed query anyway, purely so `describe_filters`
            # renders the caller's chosen types as filter chips like a parsed
            # type keyword would. Nothing keys access off this copy.
            parsed.types = set(restrict)
        if len(" ".join(raw_query.split())) < MIN_QUERY_LENGTH or parsed.is_empty:
            return SearchResponse(parsed=parsed)

        if parsed.near_me:
            # The parser has no profile/DB access, so "near me" is resolved to
            # coordinates here, once we know who is searching.
            point = profile.best_known_point()
            if point is not None:
                parsed.near_lat, parsed.near_lng = point

        response = self._run(profile, parsed, restrict=restrict, limit=limit)
        if response.total == 0 and parsed.has_structure and parsed.raw.strip():
            # Strip the same type-keyword/stopword/date noise the primary
            # parse stripped, so e.g. "photos from last summer" retries on
            # "summer" alone instead of requiring "photos" verbatim in the
            # target's own text (which defeats the point of the fallback).
            fallback_terms = extract_fallback_terms(parsed.raw)
            # Nothing meaningful survived the stripping (e.g. the whole query
            # was consumed as a type keyword plus a date phrase, like "photos
            # from last summer"). The fallback intentionally clears
            # inferred `types`/`date_start` so a wrongly-inferred place/type
            # doesn't block the retry - an explicit `restrict` is not an
            # inference and is threaded through unchanged. With *also* no
            # free-text terms,
            # `apply_text` would leave every provider's queryset unfiltered -
            # turning the retry into an unrelated recency dump across every
            # result type instead of a useful rescue. Skip it rather than
            # show that.
            if fallback_terms:
                fallback = ParsedQuery(raw=parsed.raw)
                fallback.terms = fallback_terms
                fallback.text = " ".join(fallback.terms)
                fallback_response = self._run(profile, fallback, restrict=restrict, limit=limit)
                if fallback_response.total > 0:
                    fallback_response.parsed = parsed
                    fallback_response.used_fallback = True
                    return fallback_response
        return response

    def _run(self, profile: Profile, parsed: ParsedQuery, *, restrict: frozenset[str] | None = None, limit: int | None = None) -> SearchResponse:
        """Fan one parsed query out to the applicable providers.

        Args:
            profile: The requesting user's profile.
            parsed: The query interpretation to run.
            restrict: An explicit type restriction from the caller, where empty
                means "no types" rather than "all types" - see :meth:`search`.
                None defers to ``parsed.types``.
            limit: Results per section, or None to derive it from the query's
                shape (a single-type query gets a deeper section, since it is the
                whole answer rather than one of ten).

        Returns:
            The grouped, ordered results for this one interpretation.
        """
        response = SearchResponse(parsed=parsed)
        if restrict is not None:
            active = [provider for provider in self.providers if provider.slug in restrict]
        else:
            active = [provider for provider in self.providers if not parsed.types or provider.slug in parsed.types]
        # An explicit restriction, when present, is what "how many types is this
        # search covering?" means - reading `parsed.types` instead would give the
        # plain-text fallback (which clears them) a shallow six-row section even
        # though the caller asked for exactly one type and expects its full page.
        focus = restrict if restrict is not None else parsed.types
        section_limit = limit if limit is not None else (FOCUSED_SECTION_LIMIT if len(focus) == 1 else DEFAULT_SECTION_LIMIT)

        for provider in active:
            meta = RESULT_TYPES.get(provider.slug)
            if meta is None:
                continue
            try:
                results = provider.search(profile, parsed, section_limit)
            except Exception:
                logger.exception("Global search provider '%s' failed for query %r", provider.slug, parsed.raw)
                response.errors.append(f"{meta.label} could not be searched right now.")
                continue
            if results:
                results.sort(key=lambda result: result.score, reverse=True)
                response.groups.append(SearchGroup(meta=meta, results=results))
                response.total += len(results)
        return response
