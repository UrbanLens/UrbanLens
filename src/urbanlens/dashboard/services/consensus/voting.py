"""Competitive-mode vote tallying for a round's disagreement sub-phase.
When a competitive round's submitted answers don't all agree, ``services.consensus.session`` opens a vote (``open_vote``) so every participant can pick which of the distinct submitted values is actually correct."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.consensus.model import ConsensusRoundResolution, ConsensusVote
from urbanlens.dashboard.services.consensus.fields import get_strategy

if TYPE_CHECKING:
    from urbanlens.dashboard.models.consensus.model import ConsensusAnswer, ConsensusRound
    from urbanlens.dashboard.models.profile.model import Profile


class ConsensusVotingError(Exception):
    """Raised when ``record_vote`` can't cast a vote."""


class AnswerNotInRoundError(ConsensusVotingError):
    """``chosen_answer`` doesn't belong to the round being voted on."""


class DuplicateVoteError(ConsensusVotingError):
    """``profile`` already cast a vote for this round."""


@dataclass(frozen=True)
class VoteTally:
    """The result of tallying a round's votes.

    Attributes:
        winning_answers: Every answer tied to the winning (agreeing) cluster - the "correct" value's original submitter(s) - empty when no consensus formed.
        consensus_reached: Whether the winning cluster's vote share strictly exceeded the session's configured threshold."""

    winning_answers: list[ConsensusAnswer]
    consensus_reached: bool


def open_vote(round_: ConsensusRound) -> None:
    """Transition a round into its disagreement vote sub-phase."""
    round_.resolution = ConsensusRoundResolution.VOTE_OPEN
    round_.vote_opened_at = timezone.now()
    round_.save(update_fields=["resolution", "vote_opened_at", "updated"])


def record_vote(round_: ConsensusRound, profile: Profile, chosen_answer: ConsensusAnswer) -> ConsensusVote:
    """Cast ``profile``'s vote for ``chosen_answer`` (not necessarily their own submission).

    Raises:
        AnswerNotInRoundError: ``chosen_answer`` isn't part of this round.
        DuplicateVoteError: ``profile`` already voted this round.
    """
    if chosen_answer.round_id != round_.pk:
        raise AnswerNotInRoundError(f"answer {chosen_answer.pk} belongs to round {chosen_answer.round_id}, not round {round_.pk}")
    vote, created = ConsensusVote.objects.get_or_create(round=round_, profile=profile, defaults={"chosen_answer": chosen_answer})
    if not created:
        raise DuplicateVoteError(f"profile {profile.pk} already voted in round {round_.pk}")
    return vote


def value_of(answer: ConsensusAnswer):
    """The comparable value of a submitted answer - a Point for coordinates, raw text otherwise."""
    return answer.guess_point if answer.guess_point is not None else answer.text_value


def cluster_answers(strategy, answers: list[ConsensusAnswer]) -> list[list[ConsensusAnswer]]:
    """Group ``answers`` into clusters of mutually-agreeing submissions, per ``strategy.agrees``."""
    clusters: list[list[ConsensusAnswer]] = []
    for answer in answers:
        for cluster in clusters:
            if strategy.agrees(value_of(cluster[0]), value_of(answer)):
                cluster.append(answer)
                break
        else:
            clusters.append([answer])
    return clusters


def tally_votes(round_: ConsensusRound, *, vote_threshold: float) -> VoteTally:
    """Group this round's votes by agreement and determine whether a majority formed.

    Args:
        round_: The round whose votes to tally.
        vote_threshold: The vote share (of votes actually cast, not of the full roster - an abstention isn't a "no") a cluster must strictly exceed to count as consensus.

    Returns:
        The tally."""
    strategy = get_strategy(round_.field_kind)
    answers = list(round_.answers.all())
    votes = list(ConsensusVote.objects.for_round(round_).select_related("chosen_answer"))
    total_votes = len(votes)
    if total_votes < 2 or not answers or strategy is None:
        return VoteTally(winning_answers=[], consensus_reached=False)

    clusters = cluster_answers(strategy, answers)
    vote_counts = [sum(1 for vote in votes if vote.chosen_answer_id in {answer.pk for answer in cluster}) for cluster in clusters]
    winning_index = max(range(len(clusters)), key=lambda index: vote_counts[index])
    winning_share = vote_counts[winning_index] / total_votes

    if winning_share > vote_threshold:
        return VoteTally(winning_answers=clusters[winning_index], consensus_reached=True)
    return VoteTally(winning_answers=[], consensus_reached=False)
