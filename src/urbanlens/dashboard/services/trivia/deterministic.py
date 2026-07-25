"""Deterministic trivia-question generation from cached property-records data.

Reads only ``LocationCache`` rows already populated by the property-records
plugins, via ``services.locations.site_scope.parcel_buildings()`` - never
triggers a live REData fetch, mirroring how every existing panel reading this
data works (see ``site_scope.parcel_buildings``'s own docstring: "a page
render only ever reads it"). A location with no cached data yet, or data
missing the specific fields a generator needs, simply yields no question from
that generator - this is expected, not an error, since ``year_built``/
``building_number`` are only reliably populated for CRIS-sourced (NY)
buildings today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource
from urbanlens.dashboard.services.locations import site_scope
from urbanlens.dashboard.services.locations.naming import is_meaningful_name

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location

#: "More than just a few" per the Trivia spec - deliberately stricter than
#: ``site_scope.MULTI_BUILDING_THRESHOLD`` (2), which only answers "is this a
#: multi-building parcel at all." A trivia question about the count should
#: only fire once the count is itself a genuinely interesting fact.
BUILDING_COUNT_QUESTION_THRESHOLD = 4


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
    """One question per named building with a known year built."""
    questions = []
    for building in buildings:
        name = building.get("name")
        year_built = building.get("year_built")
        if not is_meaningful_name(name) or not year_built:
            continue
        questions.append(
            _get_or_create(
                location,
                dedupe_key=f"year_built:{name}",
                prompt=f"What year was {name} built?",
                answer=str(year_built),
            ),
        )
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

    Idempotent via each question's ``dedupe_key`` - safe to call on every
    round-candidate evaluation for every location under consideration; a
    location whose cache already produced these questions just returns the
    existing rows rather than duplicating them.

    Args:
        location: The location to generate questions for.

    Returns:
        Every deterministic question now on record for this location (empty
        if there's no cached parcel-buildings data yet, or none of it met a
        generator's bar).
    """
    buildings = site_scope.parcel_buildings(location) or []
    questions = [*_year_built_questions(location, buildings), *_building_number_questions(location, buildings)]

    count_question = _building_count_question(location, buildings)
    if count_question is not None:
        questions.append(count_question)

    return questions
