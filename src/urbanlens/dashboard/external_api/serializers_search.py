"""Serializers for the external API's global-search surface.

Hand-rolled and schema-only on the response side, matching the rest of this package: nothing here is
a ``ModelSerializer``, because a search result is not a model row - it is a
:class:`~urbanlens.dashboard.services.global_search.results.SearchResult` value object assembled by
a provider, and ten different models feed into it.
Two shape decisions are load-bearing rather than stylistic:

- **``url`` is not a field.** ``SearchResult.url`` carries a *web* path (``/map/pin/<slug>/``,
  ``/wiki/<location_slug>/``) built for the search dialog's anchor...
- **Sections the credential may not read are absent, not empty.** They are named in
  ``omitted_types`` instead. A client that cannot distinguish "you have no di...
"""

from __future__ import annotations

from rest_framework import serializers

from urbanlens.dashboard.services.global_search.results import RESULT_TYPES

#: The longest query this endpoint will consider. Far above anything a search box produces, and low enough that
#: a multi-megabyte ``q`` never reaches the trigram similarity computation, which is per-row and unindexable
#: against an arbitrarily long needle.
MAX_QUERY_LENGTH = 250

#: Ceiling on the caller-requested per-section result count. The response is not paginated - it is a grouped
#: preview across up to ten domains - so this is what bounds the total work a single search can ask for.
MAX_SECTION_LIMIT = 50


class GlobalSearchQuerySerializer(serializers.Serializer):
    """Validates the ``q``/``types``/``limit`` query parameters.

    A search box fires a request per keystroke, so answering the first character with a 400 would make
    every session start with an error the user never caused.
    Those queries return an empty result set instead (see
    :class:`~urbanlens.dashboard.external_api.views_search.GlobalSearchView`).
    """

    q = serializers.CharField(required=False, allow_blank=True, max_length=MAX_QUERY_LENGTH, trim_whitespace=False)
    types = serializers.CharField(required=False, allow_blank=True, max_length=MAX_QUERY_LENGTH)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=MAX_SECTION_LIMIT)


def parse_result_types(raw: str | None) -> frozenset[str] | None:
    """Parse the ``types`` parameter into the result types to search.

    Args:
        raw: The raw comma-separated value, or None when the parameter was absent entirely.

    Returns:
        None when the caller expressed no preference (search every type the credential allows),
        otherwise the recognized subset of ``RESULT_TYPES``.
    """
    if raw is None:
        return None
    requested = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not requested:
        # An empty or whitespace-only value carries no intent to honour; treat
        # it exactly like an absent parameter.
        return None
    return frozenset(requested & RESULT_TYPES.keys())


class SearchResultSerializer(serializers.Serializer):
    """One search hit (schema-only)."""

    #: One of the ``RESULT_TYPES`` slugs; equal to the enclosing group's ``type``.
    type = serializers.CharField(read_only=True)
    title = serializers.CharField(read_only=True)
    subtitle = serializers.CharField(read_only=True, allow_blank=True)
    #: A short excerpt of the matched text, showing *why* this row matched.
    snippet = serializers.CharField(read_only=True, allow_blank=True)
    #: Material Symbols ligature, defaulted from the result's type.
    icon = serializers.CharField(read_only=True, allow_blank=True)
    image_url = serializers.CharField(read_only=True, allow_null=True)
    date = serializers.DateTimeField(read_only=True, allow_null=True)
    #: Relevance within the section. Comparable between rows of one search only -
    #: it mixes trigram similarity with a proximity bonus and has no fixed range.
    score = serializers.FloatField(read_only=True)
    #: The slug this API addresses the result (or its host) by - see
    #: ``SearchResult.object_slug``. Blank for uuid-addressed types.
    object_slug = serializers.CharField(read_only=True, allow_blank=True)
    #: The matched row's own uuid, or null for models that carry none.
    object_uuid = serializers.CharField(read_only=True, allow_null=True)


class SearchGroupSerializer(serializers.Serializer):
    """One result section: a type and its hits (schema-only)."""

    type = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    icon = serializers.CharField(read_only=True)
    results = SearchResultSerializer(many=True, read_only=True)


class GlobalSearchResponseSerializer(serializers.Serializer):
    """The full global-search payload (schema-only)."""

    #: The query exactly as submitted, echoed so a client rendering results
    #: out-of-order can discard a stale response.
    query = serializers.CharField(read_only=True, allow_blank=True)
    total = serializers.IntegerField(read_only=True)
    #: True when the structured reading of the query found nothing and these results come from a plain-text
    #: retry - worth telling the user, since the date/place filters they typed were not applied.
    used_fallback = serializers.BooleanField(read_only=True)
    #: Human-readable descriptions of the structured filters that *were* applied
    #: ("Photos", "Jun 1 - Aug 31, 2025", "in Cincinnati").
    filter_chips = serializers.ListField(child=serializers.CharField(), read_only=True)
    #: Notices for sections that errored. A failing provider degrades its own
    #: section rather than the whole search.
    errors = serializers.ListField(child=serializers.CharField(), read_only=True)
    #: Result types dropped because the calling credential lacks their scope.
    #: Not a leak: the client already knows its own grant.
    omitted_types = serializers.ListField(child=serializers.CharField(), read_only=True)
    groups = SearchGroupSerializer(many=True, read_only=True)
