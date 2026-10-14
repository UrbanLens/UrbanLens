"""Wiki merges: what a plugin that keeps rows of its own about a wiki carries across when another page absorbs it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ActionHook

if TYPE_CHECKING:
    from urbanlens.dashboard.models.wiki.model import Wiki
    from urbanlens.dashboard.services.wiki.duplicate_wikis import WikiMergeResult


WIKI_ABSORBING: ActionHook[[Wiki, Wiki, WikiMergeResult]] = ActionHook(
    "wikis.absorbing",
    "Fired with (kept, absorbed, result) after core has moved its own rows and before the absorbed page is deleted. A callback moves or snapshots its rows that refer to the absorbed page, recording any it drops in the result. Raises, so a merge never deletes a page whose rows were not carried over.",
    on_error="raise",
)

__all__ = ["WIKI_ABSORBING"]
