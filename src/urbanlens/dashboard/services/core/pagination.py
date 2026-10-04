"""Shared HTMX-friendly pagination helper for list/grid sections."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.paginator import Page, Paginator

from urbanlens.dashboard.services.core.numbers import DB_BIGINT_MAX, clamp_int

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.http import HttpRequest


def get_page(
    request: HttpRequest,
    items: Any,
    page_size: int,
    *,
    default_last: bool = False,
    param: str = "page",
) -> Page:
    """Slice ``items`` into a Django ``Page`` for the requested page number.
    Invalid or out-of-range page numbers are clamped to the nearest valid page rather than raising, so a stale pagination link can never produce an error page.

    Args:
        request: The current request; checked for a ``page`` parameter.
        items: Anything ``Paginator`` accepts - a queryset or a plain list.
        page_size: Number of items per page.
        default_last: When no ``page`` parameter is present, show the last page instead of the first.
        param: Which request parameter carries the page number.

    Returns:
        The requested ``Page`` of ``items``."""
    paginator = Paginator(items, page_size)
    page_param = request.GET.get(param) or request.POST.get(param)
    if page_param:
        return paginator.get_page(page_param)
    return paginator.get_page(paginator.num_pages if default_last else 1)


def offset_window(query: Mapping[str, Any], *, default_limit: int, max_limit: int) -> tuple[int, int]:
    """The ``offset`` and ``limit`` a grid's next-page request asks for, each one a query can be given.

    Args:
        query: The request's query parameters.
        default_limit: The page size when none, or no number, is asked for.
        max_limit: The largest page served.

    Returns:
        ``(offset, limit)``, the offset within ``[0, DB_BIGINT_MAX]`` (Postgres refuses a larger ``OFFSET``) and the
        limit within ``[1, max_limit]``.
    """
    offset = clamp_int(query.get("offset"), low=0, high=DB_BIGINT_MAX, default=0)
    limit = clamp_int(query.get("limit") or default_limit, low=1, high=max_limit, default=default_limit)
    return offset, limit
