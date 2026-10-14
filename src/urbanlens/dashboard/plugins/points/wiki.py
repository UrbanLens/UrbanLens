"""How wikis read to the people viewing them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import FilterHook

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.viewer_projection import ViewerProjection


WIKI_VIEWER_PROJECTION: FilterHook[ViewerProjection, [Wiki, Profile | None]] = FilterHook(
    "wiki.viewer_projection",
    "How one viewer reads one wiki. Receives the projection so far (core's shows the stored rows unchanged), the wiki and the viewer, and returns the projection to use: the one it was given for a viewer it has nothing to say about, or a ViewerProjection subclass. Declared to raise rather than skip, so a callback that fails cannot fall back to showing the stored rows to a viewer it should have narrowed.",
    on_error="raise",
)

__all__ = ["WIKI_VIEWER_PROJECTION"]
