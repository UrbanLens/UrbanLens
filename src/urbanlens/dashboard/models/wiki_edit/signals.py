from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.wiki_edit.model import WikiEdit


@receiver(post_save, sender=WikiEdit, dispatch_uid="consensus_award_points_on_wiki_edit")
def award_consensus_points_on_wiki_edit(sender: type[WikiEdit], instance: WikiEdit, created: bool, raw: bool = False, **kwargs) -> None:
    """Award Consensus points for any wiki edit made outside the game.

    Fires for every WikiEdit, not just ones created through Consensus, so
    users get credit for contributions "even if done outside" the game (see
    the Consensus design). What is *not* worth points - a revert, a
    Consensus-sourced edit, an edit with no editor, an empty diff - is decided
    by ``points_for_wiki_edit`` rather than by a guard here, so this handler
    stays correct if the linter strips the early return (a documented hazard
    in this codebase: write the guard so it is redundant, not load-bearing).

    Args:
        sender: The model class.
        instance: The WikiEdit that was just saved.
        created: True if a new record was created.
        raw: True during a fixture load, when related rows may not exist yet.
        **kwargs: Additional keyword arguments.
    """
    if raw or not created:
        return

    from urbanlens.dashboard.services.consensus.points import record_wiki_edit_award

    record_wiki_edit_award(instance)


@receiver(post_save, sender=WikiEdit, dispatch_uid="consensus_reconcile_points_on_revert")
def reconcile_consensus_points_on_revert(sender: type[WikiEdit], instance: WikiEdit, created: bool, raw: bool = False, **kwargs) -> None:
    """Follow an edit's ``reverted`` flag with its points.

    Reverting somebody's edit takes back what that edit paid them; reverting
    the revert puts it back. Watching the flag rather than the revert service
    catches every writer of it, including the Django admin, where ``reverted``
    is an editable field on the change form.

    The one path this cannot see is ``services.wiki.wiki_edits.revert_wiki_edit``
    clearing the flag with a queryset ``update()``, which emits no ``post_save``
    - that calls ``restore_consensus_points_for`` directly, the same split the
    reputation ledger uses.

    Both services are compare-and-swap on a flag stored on the row, so reaching
    this twice for one edit moves the total once.

    Args:
        sender: The model class.
        instance: The WikiEdit that was just saved.
        created: True if a new record was created.
        raw: True during a fixture load.
        **kwargs: Additional keyword arguments.
    """
    if raw or created:
        return

    from urbanlens.dashboard.services.consensus.points import restore_wiki_edit_award, retract_wiki_edit_award

    if instance.reverted:
        retract_wiki_edit_award(instance)
    else:
        restore_wiki_edit_award(instance)


@receiver(post_save, sender=WikiEdit, dispatch_uid="facts_record_evidence_on_wiki_edit")
def record_fact_evidence_on_wiki_edit(sender: type[WikiEdit], instance: WikiEdit, created: bool, raw: bool = False, **kwargs) -> None:
    """Log a manual wiki edit's Facts-mapped field changes as evidence.

    Skips Consensus-sourced edits: ``services.consensus.session._finish_round``
    already logs evidence for those directly from the submitted answers,
    before the wiki write even happens - logging here too would double-count
    the same observation.

    Args:
        sender: The model class.
        instance: The WikiEdit that was just saved.
        created: True if a new record was created.
        raw: True during a fixture load, when related rows may not exist yet.
        **kwargs: Additional keyword arguments.
    """
    if raw or not created or instance.editor_id is None or instance.consensus_round_id is not None:
        return

    from urbanlens.dashboard.services.facts.evidence import record_wiki_edit_evidence

    record_wiki_edit_evidence(instance)
