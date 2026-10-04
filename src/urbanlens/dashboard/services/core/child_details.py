"""Whether the page a panel request came from shows "child pin details".

A panel loaded by the page carries the setting as ``?children=``; a panel re-rendered by its own edit (a note
posted, an alias added) carries nothing. Reading the setting from the request alone made such an edit drop the
children's rows until the page reloaded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from django.urls import reverse

if TYPE_CHECKING:
    from django.http import HttpRequest

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.wiki.model import Wiki

_FLAGS = {"0", "1"}


def child_details_requested(request: HttpRequest, target: Pin | Wiki) -> bool:
    """Whether child pin or child wiki details show on the page asking for a panel.

    The request's own ``children`` flag decides when it carries one; then the page's address, which the toggle keeps
    current and htmx sends as ``HX-Current-URL``; then the page's default, as the page itself decides. Only the
    target's own page counts: a panel opened elsewhere, such as the map's label dialog, lists the target alone.

    Args:
        request: A request for one of the page's panels.
        target: The pin or wiki whose page it is.

    Returns:
        True when the children's details are shown.
    """
    explicit = request.GET.get("children") or request.POST.get("children")
    if explicit in _FLAGS:
        return explicit == "1"
    page = urlsplit(request.headers.get("HX-Current-URL") or "")
    if unquote(page.path) not in _page_paths(target):
        return False
    from_page = parse_qs(page.query).get("children") or []
    if from_page and from_page[-1] in _FLAGS:
        return from_page[-1] == "1"
    return default_child_details(target)


def _page_paths(target: Pin | Wiki) -> set[str]:
    from urbanlens.dashboard.models.pin.model import Pin

    if isinstance(target, Pin):
        return {reverse("pin.details", kwargs={"pin_slug": key}) for key in (target.slug, str(target.uuid)) if key}
    location = target.location
    if location is None:
        return set()
    return {reverse("location.wiki", kwargs={"location_slug": key}) for key in (location.slug, str(location.uuid)) if key}


def default_child_details(target: Pin | Wiki) -> bool:
    """Whether a page shows its children's details before anyone toggles them.

    Args:
        target: The pin or wiki whose page it is.

    Returns:
        For a pin, :func:`services.pins.child_buildings.child_details_default`; for a wiki, whether it describes a
        site rather than one building.
    """
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.locations.site_scope import is_site_scope

    if isinstance(target, Pin):
        from urbanlens.dashboard.services.pins.child_buildings import building_children, child_details_default

        return child_details_default(target, building_children(target).count())
    return is_site_scope(target)
