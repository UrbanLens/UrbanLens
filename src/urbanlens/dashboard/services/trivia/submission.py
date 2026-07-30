"""User-submitted trivia question intake.

A submitted question is created PENDING_REVIEW and classified
asynchronously (see services.trivia.classifier's module docstring for why
the submitter is never notified either way, by design - so they can't
iteratively probe the filter).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction

from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile

MAX_PROMPT_LENGTH = 500
MAX_ANSWER_LENGTH = 255


def submit_user_question(profile: Profile, location: Location, prompt: str, answer: str) -> TriviaQuestion:
    """Create a user-submitted question and enqueue it for classification.

    Enqueues on commit so a rolled-back transaction never schedules a task
    against a row that was never actually saved.

    Args:
        profile: The submitting profile.
        location: The location the question is about.
        prompt: The question text, truncated to MAX_PROMPT_LENGTH.
        answer: The canonical accepted answer, truncated to MAX_ANSWER_LENGTH.

    Returns:
        The new PENDING_REVIEW TriviaQuestion row.
    """
    question = TriviaQuestion.objects.create(
        location=location,
        prompt=prompt.strip()[:MAX_PROMPT_LENGTH],
        answer=answer.strip()[:MAX_ANSWER_LENGTH],
        source=TriviaQuestionSource.USER_SUBMITTED,
        status=TriviaQuestionStatus.PENDING_REVIEW,
        submitted_by=profile,
    )
    transaction.on_commit(lambda: _enqueue_classification(question.pk))
    return question


def _enqueue_classification(question_id: int) -> None:
    from urbanlens.dashboard import tasks
    from urbanlens.dashboard.services.celery import safely_enqueue_task

    safely_enqueue_task(tasks.classify_trivia_submission, question_id)


def classify_and_update(question: TriviaQuestion) -> None:
    """Run the shared content classifier on a PENDING_REVIEW question and record its verdict.

    Never raises for an AI-related failure - classify_trivia_question
    itself fails closed (returns a rejection) rather than raising. A no-op
    on a question that isn't PENDING_REVIEW, guarding against a duplicate
    task run racing a manual re-classification.

    An "ai_unavailable" verdict (AI globally/per-profile disabled, or the
    gateway call itself failed) deliberately does NOT flip the question to
    REJECTED - that would silently mass-reject every submission for the
    duration of any AI outage or an admin's deliberate AI-off period. The
    question is left PENDING_REVIEW instead, ready to be picked up by a
    later classification run once AI is available again.

    Args:
        question: The question to classify.
    """
    from urbanlens.dashboard.services.trivia.classifier import classify_trivia_question

    if question.status != TriviaQuestionStatus.PENDING_REVIEW:
        return

    verdict = classify_trivia_question(question.prompt, question.answer, question.location, profile=question.submitted_by)
    if verdict.reason == "ai_unavailable":
        return

    question.status = TriviaQuestionStatus.APPROVED if verdict.approved else TriviaQuestionStatus.REJECTED
    question.rejection_reason = verdict.reason
    question.save(update_fields=["status", "rejection_reason", "updated"])
