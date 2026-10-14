"""Moderation of user-submitted trivia questions: whether a question may enter rotation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class ModerationVerdict:
    """A moderator's decision. ``reason`` is recorded on a rejected question."""

    approved: bool
    reason: str | None = None


class TriviaQuestionModerator(ABC):
    """Decides whether a submitted question may enter rotation, from its prompt, accepted answer and location."""

    @abstractmethod
    def moderate(self, prompt: str, answer: str, location: Location, *, profile: Profile | None) -> ModerationVerdict | None:
        """
        Return a verdict, or None to abstain, leaving the next moderator to decide.

        Args:
            prompt: The question text.
            answer: The accepted answer.
            location: The location the question is about.
            profile: The submitting profile, when there is one.
        """


def moderate_question(prompt: str, answer: str, location: Location, *, profile: Profile | None) -> ModerationVerdict | None:
    """Ask the moderators in priority order; the first verdict decides, and None means none did."""
    from urbanlens.dashboard.plugins.points import TRIVIA_QUESTION_MODERATORS

    for moderator in TRIVIA_QUESTION_MODERATORS.items():
        verdict = moderator.moderate(prompt, answer, location, profile=profile)
        if verdict is not None:
            return verdict
    return None
