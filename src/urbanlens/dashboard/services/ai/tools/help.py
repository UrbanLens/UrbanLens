"""The assistant's "how do I…" lookup tool - grounded in services.ai.page_help.PAGE_HELP only."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register


class GetPageHelpArgs(BaseModel):
    page: str = Field(max_length=100)


def _get_page_help(context: ToolContext, args: GetPageHelpArgs) -> dict[str, Any]:
    from urbanlens.dashboard.services.ai.page_help import get_page_help

    help_ = get_page_help(args.page.strip())
    if help_ is None:
        return {"error": f"No help is available for {args.page!r}. Only answer 'how do I' from this tool's own output - never guess."}
    return {"title": help_.title, "key_actions": list(help_.key_actions), "tips": list(help_.tips)}


register(
    ToolSpec(
        name="get_page_help",
        description=(
            "Look up how-to help for one of UrbanLens's pages by its URL name - one of: "
            "home.view, map.view, organize.index, trips.overview, memories.view, vault.home, "
            "safety.home, games.overview, pin.details, trips.detail, settings.view. "
            "This is the only source of 'how do I…' answers - never answer from anything else."
        ),
        args_model=GetPageHelpArgs,
        handler=_get_page_help,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.NONE,
        progress_label="Looking up help…",
        action_label="Looked up page help",
    ),
)
