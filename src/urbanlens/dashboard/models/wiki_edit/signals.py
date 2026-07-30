from django.db.models.signals import post_save
from django.dispatch import receiver

from urbanlens.dashboard.models.wiki_edit.model import WikiEdit


@receiver(post_save, sender=WikiEdit, dispatch_uid="consensus_award_points_on_wiki_edit")
def award_consensus_points_on_wiki_edit(sender: type[WikiEdit], instance: WikiEdit, created: bool, **kwargs) -> None:
    """Award baseline Consensus points for any wiki edit made outside the game.

    Fires for every WikiEdit, not just ones created through Consensus, so
    users get credit for contributions "even if done outside" the game (see
    the Consensus design). ``consensus_round`` is the double-award guard: a
    Consensus-sourced edit already got its (larger, in-game) points at
    resolution time (``services.consensus.session``), so this only ever
    fires for edits made through the ordinary wiki-edit UI.

    Args:
        sender: The model class.
        instance: The WikiEdit that was just saved.
        created: True if a new record was created.
        **kwargs: Additional keyword arguments.
    """
    if not created or instance.consensus_round_id is not None or instance.editor_id is None:
        return

    from urbanlens.dashboard.services.consensus.points import award_points_for_manual_edit

    award_points_for_manual_edit(instance.editor_id)


@receiver(post_save, sender=WikiEdit, dispatch_uid="facts_record_evidence_on_wiki_edit")
def record_fact_evidence_on_wiki_edit(sender: type[WikiEdit], instance: WikiEdit, created: bool, **kwargs) -> None:
    """Log a manual wiki edit's Facts-mapped field changes as evidence.

    Skips Consensus-sourced edits: ``services.consensus.session._finish_round``
    already logs evidence for those directly from the submitted answers,
    before the wiki write even happens - logging here too would double-count
    the same observation.

    Args:
        sender: The model class.
        instance: The WikiEdit that was just saved.
        created: True if a new record was created.
        **kwargs: Additional keyword arguments.
    """
    if not created or instance.editor_id is None or instance.consensus_round_id is not None:
        return

    from urbanlens.dashboard.services.facts.evidence import record_wiki_edit_evidence

    record_wiki_edit_evidence(instance)
