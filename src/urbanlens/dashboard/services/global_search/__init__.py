"""Site-wide global search.

Searches everything a user has submitted or has direct access to - pins,
photos (including plugin-generated photo keywords), wikis, trips, visits,
direct messages, safety check-ins, markup maps, and comments - with
typo-tolerant matching (PostgreSQL trigram similarity) and lightweight
natural-language parsing ("photos from last summer", "pins in Cincinnati").
"""

from urbanlens.dashboard.services.global_search.engine import GlobalSearchEngine, SearchGroup, SearchResponse
from urbanlens.dashboard.services.global_search.parser import ParsedQuery, parse_query
from urbanlens.dashboard.services.global_search.results import RESULT_TYPES, ResultTypeMeta, SearchResult

__all__ = [
    "RESULT_TYPES",
    "GlobalSearchEngine",
    "ParsedQuery",
    "ResultTypeMeta",
    "SearchGroup",
    "SearchResponse",
    "SearchResult",
    "parse_query",
]
