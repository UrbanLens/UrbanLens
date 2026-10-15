"""What people contribute, and when they take it back: events a plugin can account for."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import ActionHook

if TYPE_CHECKING:
    from django.db.models import Model

    from urbanlens.dashboard.models.friendship.invitation.model import FriendInvitation
    from urbanlens.dashboard.models.wiki.model import Wiki


INVITATION_ACCEPTED: ActionHook[[FriendInvitation]] = ActionHook(
    "friends.invitation_accepted",
    "Fired when a friend invitation is claimed, once per invitation. That claim is a conditional queryset update, so no model signal reports it. A raising callback propagates.",
    on_error="raise",
)

CONTRIBUTION_WITHDRAWN: ActionHook[[Model]] = ActionHook(
    "contributions.withdrawn",
    "Fired just before a contributor ends something they contributed (deletes their comment, takes their photo off a wiki), with the contribution; it still has its primary key. Never fired for removals by anyone else. A raising callback propagates.",
    on_error="raise",
)

WIKI_EDITS_RESTORED: ActionHook[[list[int]]] = ActionHook(
    "wiki.edits_restored",
    "Fired after the reverts of some wiki edits were themselves undone, with those edits' primary keys. The edits' flags are cleared by a queryset update, which sends no ``post_save``. A raising callback propagates.",
    on_error="raise",
)

WIKIS_MERGED: ActionHook[[Wiki, Wiki]] = ActionHook(
    "wiki.merged",
    "Fired inside the merge transaction while a duplicate wiki is folded into another, before references are repointed and the duplicate is deleted. Receives the wiki that stays, then the one being absorbed. A raising callback propagates and rolls the merge back.",
    on_error="raise",
)

__all__ = ["CONTRIBUTION_WITHDRAWN", "INVITATION_ACCEPTED", "WIKIS_MERGED", "WIKI_EDITS_RESTORED"]
