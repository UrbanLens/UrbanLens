"""Shared template components for the Memories section.

* ``{% memories_tabs active %}`` renders the Timeline | Photos | Maps |
  Sharing | Visits tab strip (``partials/memories/_photos_tabs.html``). The
  tag computes the unlogged-visits count itself, so every Memories subpage
  shows an identical nav without each view having to supply the count.
"""

from __future__ import annotations

from typing import Any

from django import template

register = template.Library()


@register.inclusion_tag("dashboard/partials/memories/_photos_tabs.html", takes_context=True)
def memories_tabs(context: template.Context, active: str) -> dict[str, Any]:
    """Render the Memories section's tab strip.

    The "Visits" tab only appears when the viewer has visited-but-unlogged
    pins; the count is reused from the page context when the view already
    fetched it (e.g. for the unlogged-visits band), and computed here
    otherwise so no subpage can accidentally drop the tab.

    Args:
        context: The calling template's context (used for ``request`` and an
            optional prefetched ``unlogged_visits`` list).
        active: Which tab is current - ``"timeline"``, ``"photos"``,
            ``"maps"``, ``"sharing"``, or ``"visits"``.

    Returns:
        Context for ``partials/memories/_photos_tabs.html``.
    """
    unlogged = context.get("unlogged_visits")
    if unlogged is not None:
        count = len(unlogged)
    else:
        count = 0
        request = context.get("request")
        if request is not None and request.user.is_authenticated:
            from urbanlens.dashboard.models.profile.model import Profile
            from urbanlens.dashboard.services.memories.unlogged import unlogged_visited_pins

            profile, _ = Profile.objects.get_or_create(user=request.user)
            count = len(unlogged_visited_pins(profile))
    return {"active": active, "unlogged_visits_count": count}
