"""Preserve reply thread context when a parent comment is deleted (UL-219)."""

from __future__ import annotations

from django.db.models.signals import pre_delete
from django.dispatch import receiver

from urbanlens.dashboard.models.comments.model import Comment


@receiver(pre_delete, sender=Comment, dispatch_uid="comment_flag_parent_deleted_on_delete")
def flag_replies_on_parent_delete(sender: type[Comment], instance: Comment, **kwargs) -> None:
    """Mark every reply of a comment about to be deleted as parent-deleted.

    Runs pre-delete (rather than post-delete) so the affected replies are
    flagged before Django's collector nulls out their ``parent`` FK via
    ``on_delete=SET_NULL`` - by post-delete time there is no reliable way to
    find them again (mirrors ``markup.signals.flag_map_removed_on_map_delete``).
    Uses bulk ``.update()`` rather than per-row ``.save()`` so this doesn't
    re-trigger any signal handlers on the reply rows themselves.
    """
    Comment.objects.filter(parent=instance).update(parent_deleted=True)
