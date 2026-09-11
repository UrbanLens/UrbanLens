"""Shared HTMX-friendly pagination helper for list/grid sections."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core.paginator import Page, Paginator

if TYPE_CHECKING:
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
        default_last: When no ``page`` parameter is present, show the last
            page instead of the first. Useful for sections ordered oldest
            to newest (e.g. comments) where the most recent items should be
            visible by default.
        param: Which request parameter carries the page number. Worth
            overriding when a single page renders two paginated sections: they
            each fetch their own partial by its own URL, so their HTMX
            pagination is independent, but the *first* render is one request
            and a shared ``?page=`` would move both of them at once.

    Returns:
        The requested ``Page`` of ``items``."""
    paginator = Paginator(items, page_size)
    page_param = request.GET.get(param) or request.POST.get(param)
    if page_param:
        return paginator.get_page(page_param)
    return paginator.get_page(paginator.num_pages if default_last else 1)
