"""Judging a trivia answer that does not exactly match the accepted one."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class AnswerJudgement:
    """A judge's ruling on a non-exact answer. ``match_kind`` is what a correct answer records as how it matched."""

    equivalent: bool
    match_kind: str


class TriviaAnswerJudge(ABC):
    """Rules on whether an answer that is not an exact match means the same as the accepted answer."""

    @abstractmethod
    def judge(self, raw_answer: str, accepted_answer: str, *, profile: Profile) -> AnswerJudgement | None:
        """
        Return a ruling, or None to abstain, leaving the next judge to decide.

        Args:
            raw_answer: The answer as typed.
            accepted_answer: The question's canonical answer.
            profile: The answering profile.
        """


def judge_answer(raw_answer: str, accepted_answer: str, *, profile: Profile) -> AnswerJudgement | None:
    """Ask the judges in priority order; the first ruling decides, and None means none did."""
    from urbanlens.dashboard.plugins.points import TRIVIA_ANSWER_JUDGES

    for judge in TRIVIA_ANSWER_JUDGES.items():
        judgement = judge.judge(raw_answer, accepted_answer, profile=profile)
        if judgement is not None:
            return judgement
    return None
