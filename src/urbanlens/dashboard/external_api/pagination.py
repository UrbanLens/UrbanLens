"""Page-number pagination for the external API's *browse* list endpoints.

Deliberately distinct from the pin sync feed (``services.pin_sync``, served by
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

from rest_framework.pagination import PageNumberPagination

if TYPE_CHECKING:
    from django.db.models import QuerySet
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
    ) -> Response:
        """Serialize one page of *queryset* into the standard envelope.

        Args:
            queryset: The ordered rows to page through. Must have a
                deterministic ordering, or pages will overlap and drop rows.
            serializer_class: The serializer to apply to the page, instantiated
                with ``many=True``.
            request: The request whose ``page``/``page_size`` params drive
                pagination.
            context: Extra serializer context. Used to hand a serializer the
                parent object its fields need (e.g. the pin an alias belongs
                to), so a whole page resolves without a query per row.

        Returns:
            A ``{count, next, previous, results}`` response for the requested
            page.
        """
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request, view=self)
        serializer = serializer_class(page, many=True, context=context or {})
        return paginator.get_paginated_response(serializer.data)
