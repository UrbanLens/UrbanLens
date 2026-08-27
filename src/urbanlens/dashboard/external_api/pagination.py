"""Page-number pagination for the external API's *browse* list endpoints.

Deliberately distinct from the pin sync feed (``services.pins.pin_sync``, served by
``views.PinsView``/``PinTombstonesView``), which is cursor-based on
``(updated, pk)`` and hands back a watermark so a client can ask "what changed
since?" and never miss a row that shifted position mid-walk. That feed must not
be converted to this: delta sync and browsing are different problems, and
page numbers are unsound for the former precisely because concurrent edits
reorder pages under the reader.

Everything else - a user's lists, labels, trips, photos, conversations - is
browsed rather than synced, and for those a plain
``{count, next, previous, results}`` envelope is what a paging UI wants.

Because ``ExternalApiView`` extends DRF's plain ``APIView`` (not
``GenericAPIView``), views don't inherit ``paginate_queryset``. Use
:class:`PaginatedListMixin` to get the same behavior without changing base
classes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db.models import QuerySet
from rest_framework.pagination import PageNumberPagination

if TYPE_CHECKING:
    from collections.abc import Callable

    from rest_framework.request import Request
    from rest_framework.response import Response
    from rest_framework.serializers import BaseSerializer


class ExternalApiPagination(PageNumberPagination):
    """The external API's standard page-number pagination.

    ``page_size`` is a compromise between round-trips and payload size on a
    mobile connection; ``max_page_size`` bounds what a caller can ask for, so
    ``?page_size=100000`` degrades to 100 rather than attempting to serialize
    an entire table.
    """

    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def stable_ordering(queryset: QuerySet[Any] | list[Any]) -> QuerySet[Any] | list[Any]:
    """Append a primary-key tie-break so paging cannot repeat or drop rows.

    Page-number pagination is ``LIMIT``/``OFFSET`` underneath. Postgres may return
    rows with equal sort keys in any order, and nothing obliges it to choose the
    same order for the query behind page 2 as for page 1 - so with a tied ordering
    a row can appear on both pages while another appears on neither, silently.
    Several orderings in this project end in a plain ``CharField``
    (``WikiOwner.name``, ``PinAlias.name``, ``Album.name``), which ties readily.

    Left alone deliberately:

    - **Lists.** Already materialised in a fixed order by their caller.
    - **``distinct()`` queries.** ``SELECT DISTINCT`` requires every ``ORDER BY``
      term to appear in the select list, so adding ``pk`` to a ``.values()``
      projection that omits it is a database error, not a fix.
    - **Aggregate queries.** Django folds ``ORDER BY`` terms into ``GROUP BY``;
      appending ``pk`` would split every group into one row per row and change the
      numbers the endpoint reports.
    - **Orderings already ending in the primary key**, which are deterministic.
    - **Sliced querysets.** Django raises ``TypeError: Cannot reorder a query once
      a slice has been taken``, so reordering here would turn a working endpoint
      into a 500. A caller that has already sliced has also already decided which
      rows it wants, which is the thing pagination would otherwise be choosing.
    - **Combined querysets** (``union``/``intersection``/``difference``), whose
      ``ORDER BY`` may only name columns in the combined select list. ``pk`` is
      usually there, but "usually" is not worth a 500 on an endpoint that works.

    Args:
        queryset: The rows about to be paginated, or a pre-built list.

    Returns:
        The same rows with a deterministic ordering, or the input unchanged when
        one of the cases above applies.
    """
    if not isinstance(queryset, QuerySet):
        return queryset
    query = queryset.query
    if query.distinct or query.distinct_fields or query.group_by:
        return queryset
    if query.is_sliced or query.combinator:
        return queryset
    meta = queryset.model._meta  # noqa: SLF001 - _meta is public API despite the underscore
    ordering = list(query.order_by) or list(meta.ordering or [])
    if any(term.lstrip("-") in ("pk", meta.pk.name) for term in ordering if isinstance(term, str)):
        return queryset
    return queryset.order_by(*ordering, "pk")


class PaginatedListMixin:
    """Adds one-call paginated list responses to an ``APIView``-based external view.

    ``ExternalApiView`` extends ``APIView`` rather than ``GenericAPIView``
    (the external surface is hand-rolled end to end - see the serializers
    module docstring for why), so it has no ``paginate_queryset``. This mixin
    supplies the equivalent without pulling in the generic view machinery and
    the implicit queryset/serializer attributes that come with it.
    """

    #: Overridable per view, e.g. for an endpoint needing a different page size.
    pagination_class: type[PageNumberPagination] = ExternalApiPagination

    def paginated_response(
        self,
        queryset: QuerySet[Any] | list[Any],
        serializer_class: type[BaseSerializer],
        request: Request,
        *,
        context: dict[str, Any] | None = None,
        row_builder: Callable[[Any], Any] | None = None,
    ) -> Response:
        """Serialize one page of *queryset* into the standard envelope.

        Args:
            queryset: The rows to page through. A primary-key tie-break is
                appended by ``stable_ordering`` where it is safe to do so, so a
                caller whose ordering ties does not silently lose rows across
                pages; see that function for the cases it must leave alone.
            serializer_class: The serializer to apply to the page, instantiated
                with ``many=True``.
            request: The request whose ``page``/``page_size`` params drive
                pagination.
            context: Extra serializer context. Used to hand a serializer the
                parent object its fields need (e.g. the pin an alias belongs
                to), so a whole page resolves without a query per row.
            row_builder: Optional per-row shaping applied *after* pagination,
                for endpoints whose payload is a dict built from a model rather
                than the model itself. Passing a queryset plus this is strictly
                better than pre-building a list of dicts and paginating that:
                the list comprehension form evaluates the whole queryset and
                does its per-row work (resolving storage URLs, following
                relations) for every row on every request, no matter which page
                was asked for, so the page-size limit stops bounding anything
                but the response body. Anything the builder needs that would
                cost a query per row belongs in the queryset's
                ``select_related``/``prefetch_related`` instead.

        Returns:
            A ``{count, next, previous, results}`` response for the requested
            page.
        """
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(stable_ordering(queryset), request, view=self)
        rows = [row_builder(item) for item in page] if row_builder is not None else page
        serializer = serializer_class(rows, many=True, context=context or {})
        return paginator.get_paginated_response(serializer.data)
