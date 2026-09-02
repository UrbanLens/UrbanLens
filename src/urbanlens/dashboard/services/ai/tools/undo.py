"""The assistant's undo tools - a two-step peek-then-confirm pair, never a bare "undo that".

``undo_last_action`` is a write: the loop never runs it (``registry.execute``'s
own ``confirmed=False`` path turns it into a proposal), and that proposal's
``args`` are exactly what the model passed - the ``undo_uuid`` from a prior
``undo_peek`` call, not anything the handler computes. The confirm endpoint
re-verifies that uuid against the *current* top of the stack before restoring
anything, so a user who does something else between asking and confirming
can't have that newer action undone in place of the one they were told about.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.services.ai.tools.registry import DataScope, ToolContext, ToolSpec, register


class UndoPeekArgs(BaseModel):
    pass


def _undo_peek(context: ToolContext, _args: UndoPeekArgs) -> dict[str, Any]:
    from urbanlens.dashboard.services.undo.service import peek_undo

    action = peek_undo(context.profile)
    if action is None:
        return {"can_undo": False}
    return {"can_undo": True, "label": action.object_repr, "undo_uuid": str(action.uuid)}


register(
    ToolSpec(
        name="undo_peek",
        description="Check whether the user has a recent action they could undo, and what it was. Always call this before offering to undo anything, or before calling undo_last_action.",
        args_model=UndoPeekArgs,
        handler=_undo_peek,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.OWN_PROFILE,
        progress_label="Checking your undo history…",
        action_label="Checked your undo history",
    ),
)


class UndoLastActionArgs(BaseModel):
    undo_uuid: str = Field(max_length=36, description="The undo_uuid an undo_peek call in this same turn returned - never guess or reuse one from an earlier turn.")


def _undo_last_action(context: ToolContext, args: UndoLastActionArgs) -> dict[str, Any]:
    from urbanlens.dashboard.services.undo.service import UndoExpiredError, peek_undo, restore_undo_action

    action = peek_undo(context.profile)
    if action is None or str(action.uuid) != args.undo_uuid:
        return {"error": "That's no longer the most recent undoable action - call undo_peek again to see what's current, then confirm with its uuid."}
    try:
        restore_undo_action(action)
    except UndoExpiredError:
        return {"error": "That undo has expired."}
    return {"status": "undone", "label": action.object_repr}


register(
    ToolSpec(
        name="undo_last_action",
        description="Undo the user's most recent undoable action. Requires the undo_uuid an undo_peek call in this same turn returned - call undo_peek first if you don't already have one.",
        args_model=UndoLastActionArgs,
        handler=_undo_last_action,
        read_only=False,
        requires_confirmation=True,
        features=frozenset({SiteFeature.AI}),
        scope=DataScope.OWN_PROFILE,
        progress_label="Undoing…",
        action_label="Undid it",
        confirm_label="Undo",
    ),
)
