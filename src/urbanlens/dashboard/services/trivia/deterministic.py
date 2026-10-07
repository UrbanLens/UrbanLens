"""Deterministic trivia-question generation from cached property-records data."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus
from urbanlens.dashboard.services.locations import site_scope
from urbanlens.dashboard.services.locations.naming import is_meaningful_name
from urbanlens.dashboard.services.pins.build_dates import own_build_year

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

#: "More than just a few" per the Trivia spec - deliberately stricter than
#: ``site_scope.MULTI_BUILDING_THRESHOLD`` (2), which only answers "is this a multi-building parcel
#: at all." A trivia question about the count should only fire once the count is itself a genuinely
#: interesting fact.
BUILDING_COUNT_QUESTION_THRESHOLD = 4

#: ``dedupe_key`` prefix of a year-built question.
YEAR_BUILT_KEY = "year_built:"
#: ``rejection_reason`` of a year-built question withdrawn because the building records no longer date its building.
#: Such a question is approved again once they do; a question rejected for any other reason is left as it is.
#: Migration 0070 writes the same text.
YEAR_BUILT_WITHDRAWN = "The building records do not date this building itself."


def _get_or_create(location: Location, *, dedupe_key: str, prompt: str, answer: str) -> TriviaQuestion:
    """Idempotently materialize one deterministic question, keyed by ``(location, dedupe_key)``."""
    question, _ = TriviaQuestion.objects.get_or_create(
        location=location,
        dedupe_key=dedupe_key,
        defaults={
            "prompt": prompt,
            "answer": answer,
            "source": TriviaQuestionSource.DETERMINISTIC,
        },
    )
    return question


def _year_built_questions(location: Location, buildings: list[dict]) -> list[TriviaQuestion]:
    """One question per named building whose records date that building itself.

    A year that is the parcel's, or whose basis REData did not say (``services.pins.build_dates.own_build_year``),
    would assert one building's year from its parcel's, so it asks nothing. A question the records no longer support
    is withdrawn, and one withdrawn that way is approved again, with the records' year, once they do.
    """
    wanted: dict[str, tuple[str, str]] = {}
    for building in buildings:
        name = building.get("name")
        year = own_build_year(building)
        if is_meaningful_name(name) and year is not None:
            wanted.setdefault(f"{YEAR_BUILT_KEY}{name}", (str(name), str(year)))

    asked = TriviaQuestion.objects.filter(location=location, source=TriviaQuestionSource.DETERMINISTIC, dedupe_key__startswith=YEAR_BUILT_KEY)
    asked.filter(status=TriviaQuestionStatus.APPROVED).exclude(dedupe_key__in=wanted).update(status=TriviaQuestionStatus.REJECTED, rejection_reason=YEAR_BUILT_WITHDRAWN, updated=timezone.now())

    questions = []
    for dedupe_key, (name, answer) in wanted.items():
        question = _get_or_create(location, dedupe_key=dedupe_key, prompt=f"What year was {name} built?", answer=answer)
        withdrawn = question.status == TriviaQuestionStatus.REJECTED and question.rejection_reason == YEAR_BUILT_WITHDRAWN
        if question.status != TriviaQuestionStatus.APPROVED and not withdrawn:
            continue
        if withdrawn or question.answer != answer:
            question.answer, question.status, question.rejection_reason = answer, TriviaQuestionStatus.APPROVED, None
            question.save(update_fields=["answer", "answer_normalized", "status", "rejection_reason", "updated"])
        questions.append(question)
    return questions


def _building_number_questions(location: Location, buildings: list[dict]) -> list[TriviaQuestion]:
    """One question per named building with a known building number."""
    questions = []
    for building in buildings:
        name = building.get("name")
        building_number = building.get("building_number")
        if not is_meaningful_name(name) or not building_number:
            continue
        questions.append(
            _get_or_create(
                location,
                dedupe_key=f"building_number:{name}",
                prompt=f"What is the building number of {name}?",
                answer=str(building_number),
            ),
        )
    return questions


def _building_count_question(location: Location, buildings: list[dict]) -> TriviaQuestion | None:
    """A question about how many buildings sit on this parcel, only when that count is notable."""
    count = len(buildings)
    if count < BUILDING_COUNT_QUESTION_THRESHOLD:
        return None
    return _get_or_create(
        location,
        dedupe_key="building_count",
        prompt="How many buildings are on this parcel?",
        answer=str(count),
    )


def generate_deterministic_questions(location: Location) -> list[TriviaQuestion]:
    """Generate (or return already-generated) deterministic questions for ``location``.

    Args:
        location: The location to generate questions for.

    Returns:
        Every deterministic question now on record for this location (empty if there's no cached parcel-buildings data yet, or none of it met a generator's bar)."""
    buildings = site_scope.parcel_buildings(location)
    if buildings is None:
        # Nothing to judge a year-built question asked earlier by, so it stays as it is.
        return []
    questions = [*_year_built_questions(location, buildings), *_building_number_questions(location, buildings)]

    count_question = _building_count_question(location, buildings)
    if count_question is not None:
        questions.append(count_question)

    return questions
