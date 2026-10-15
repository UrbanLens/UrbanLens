"""Trivia: how questions are moderated, how answers are judged, and which kinds of each exist."""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.extensions import Choices, ExtensionPoint

if TYPE_CHECKING:
    from urbanlens.dashboard.services.trivia.judging import TriviaAnswerJudge
    from urbanlens.dashboard.services.trivia.moderation import TriviaQuestionModerator


TRIVIA_QUESTION_SOURCES = Choices(
    "trivia.question_sources",
    "Where a trivia question can come from, beyond core's ``TriviaQuestionSource`` members, as ``value -> label``.",
)

TRIVIA_ANSWER_MATCH_KINDS = Choices(
    "trivia.answer_match_kinds",
    "How an answer can be judged correct, beyond core's ``TriviaAnswerMatchKind`` members, as ``value -> label``.",
)

TRIVIA_QUESTION_MODERATORS: ExtensionPoint[TriviaQuestionModerator] = ExtensionPoint(
    "trivia.question_moderators",
    "Moderators asked in priority order whether a submitted question may enter rotation. The first to return a verdict decides; with none, a submission stays pending review.",
)

TRIVIA_ANSWER_JUDGES: ExtensionPoint[TriviaAnswerJudge] = ExtensionPoint(
    "trivia.answer_judges",
    "Judges asked in priority order whether an answer that is not an exact match is correct anyway. The first to rule decides; with none, only an exact match is correct.",
)

__all__ = ["TRIVIA_ANSWER_JUDGES", "TRIVIA_ANSWER_MATCH_KINDS", "TRIVIA_QUESTION_MODERATORS", "TRIVIA_QUESTION_SOURCES"]
