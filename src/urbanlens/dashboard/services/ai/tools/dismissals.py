"""The assistant's "what did I just dismiss" tools - grounded only in the client's own ring for this turn (services.ai.dismissals), never a server-side lookup."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register


class RecentDismissalsArgs(BaseModel):
    pass


def _recent_dismissals(context: ToolContext, _args: RecentDismissalsArgs) -> dict[str, Any]:
    return {"dismissals": [{"id": entry.id, "kind": entry.kind, "heading": entry.heading, "body": entry.body, "page": entry.page} for entry in context.dismissals]}


register(
    ToolSpec(
        name="recent_dismissals",
        description="List the page explainers and onboarding-tour cards the user has recently dismissed - from what the client just reported, not a database record. Use this before answering 'what did I just dismiss/close'.",
        args_model=RecentDismissalsArgs,
        handler=_recent_dismissals,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.NONE,
        user_content_fields=frozenset({"heading", "body"}),
        progress_label="Checking what you dismissed…",
        action_label="Checked what you dismissed",
    ),
)


class ReopenExplainerArgs(BaseModel):
    id: str = Field(max_length=200)


def _reopen_explainer(context: ToolContext, args: ReopenExplainerArgs) -> dict[str, Any]:
    entry = next((candidate for candidate in context.dismissals if candidate.id == args.id), None)
    if entry is None:
        return {"error": f"No recently dismissed item with id {args.id!r} - call recent_dismissals first to see what's available."}
    return {"status": "reopened", "id": entry.id, "kind": entry.kind, "page": entry.page, "prefix": entry.prefix}


register(
    ToolSpec(
        name="reopen_explainer",
        description=("Reopen a dismissed page explainer, or restart a dismissed onboarding-tour card, by its id from recent_dismissals. Only works for something the user dismissed this session - call recent_dismissals first if unsure of the id."),
        args_model=ReopenExplainerArgs,
        handler=_reopen_explainer,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.NONE,
        client_action="reopen_explainer",
        progress_label="Reopening…",
        action_label="Reopened it",
    ),
)
