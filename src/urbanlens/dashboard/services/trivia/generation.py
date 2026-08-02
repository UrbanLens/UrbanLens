"""AI-generated trivia questions mined from wiki article content.

Every wiki already implies at least one profile can see it (a Wiki only
ever exists because its creator had pinned that location - see
``services.wiki.wiki_access``'s visibility rule) - so, unlike a per-viewer
request, this background generator needs no additional profile-scoping
before reading a wiki's article text.

Generated questions are classified by the exact same
``services.trivia.classifier`` used for user submissions before ever being
persisted - a rejected candidate was never shown to anyone, so (unlike a
user's own rejected submission) there is no "show it back to the author
very rarely" leniency to apply here; it is simply discarded.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models.functions import Length

from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus
from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.scanner import wrap_user_data
from urbanlens.dashboard.services.trivia.classifier import classify_trivia_question

if TYPE_CHECKING:
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: A wiki's description must be at least this long before it's considered to
#: have "substantial content" worth mining for trivia questions.
MIN_DESCRIPTION_LENGTH = 400

#: Upper bound on how many candidate questions one wiki generates per run.
MAX_QUESTIONS_PER_WIKI = 3

#: How many not-yet-processed wikis one sweep considers - bounded so a
#: single scheduled run can't spend unbounded AI tokens.
DEFAULT_SWEEP_BATCH_SIZE = 25

#: Separator between a generated question and its answer within one ANSWER
#: tag - deliberately unlikely to appear in ordinary prose.
_PAIR_SEPARATOR = "|||"

_INSTRUCTIONS = f"""You write trivia questions about real-world locations from their wiki article text. You will be given one location's article. Write up to {MAX_QUESTIONS_PER_WIKI} trivia questions and their answers, based ONLY on facts stated in the article - never invent facts.

Each question must be about the location itself - its history, architecture, construction, ownership, geography, or similar objective facts. Do not write a question about a specific person, even one only mentioned in the article - focus only on the place. Do not write a question about specific exploring groups, visits, or parties.

Each answer must be short (a date, a number, a name, or a few words) - not a sentence.

Respond with each question/answer pair wrapped in its own ANSWER tag, with the question and answer separated by "{_PAIR_SEPARATOR}", like this:
<ANSWER>What year was the main building constructed?{_PAIR_SEPARATOR}1924</ANSWER>

If the article doesn't contain enough concrete facts to write a good question, return no ANSWER tags at all."""


def generate_questions_for_wiki(wiki: Wiki) -> list[TriviaQuestion]:
    """Generate, classify, and persist approved AI trivia questions from one wiki's article.

    Idempotent per location: a location that already has at least one
    AI_GENERATED question is skipped entirely, so this is safe to call
    repeatedly (e.g. from a periodic sweep) without regenerating or
    re-spending tokens on the same wiki.

    Args:
        wiki: The wiki to mine for trivia questions.

    Returns:
        Every newly-created (APPROVED) question - empty if the wiki was
        skipped (no substantial content, already generated, AI unavailable)
        or nothing survived classification.
    """
    if TriviaQuestion.objects.filter(location=wiki.location, source=TriviaQuestionSource.AI_GENERATED).exists():
        return []
    if not wiki.description or len(wiki.description) < MIN_DESCRIPTION_LENGTH:
        return []

    gateway = get_gateway("trivia_generation", instructions=_INSTRUCTIONS)
    if gateway is None:
        return []

    prompt = wrap_user_data(wiki.description)
    try:
        raw_pairs = gateway.send_prompt_list(prompt, max_results=MAX_QUESTIONS_PER_WIKI)
    except Exception:
        # A transport-level failure must never bubble up out of a scheduled
        # sweep task and abort the whole batch - just skip this wiki, same
        # as "AI unavailable."
        logger.exception("Trivia generation call failed unexpectedly for wiki %s; skipping", wiki.pk)
        return []

    created: list[TriviaQuestion] = []
    for raw_pair in raw_pairs:
        if _PAIR_SEPARATOR not in raw_pair:
            logger.warning("Trivia generation returned a pair with no separator; discarding: %r", raw_pair)
            continue
        question_text, _, answer_text = raw_pair.partition(_PAIR_SEPARATOR)
        question_text, answer_text = question_text.strip(), answer_text.strip()
        if not question_text or not answer_text:
            continue

        verdict = classify_trivia_question(question_text, answer_text, wiki.location)
        if not verdict.approved:
            logger.info("AI-generated trivia question rejected (%s): %r", verdict.reason, question_text)
            continue

        created.append(
            TriviaQuestion.objects.create(
                location=wiki.location,
                prompt=question_text[:500],
                answer=answer_text[:255],
                source=TriviaQuestionSource.AI_GENERATED,
                status=TriviaQuestionStatus.APPROVED,
            ),
        )
    return created


def sweep_wikis_for_generation(*, batch_size: int = DEFAULT_SWEEP_BATCH_SIZE) -> dict[str, int]:
    """Generate AI trivia questions for a bounded batch of not-yet-processed wikis.

    Called from a scheduled Celery task (``tasks.run_scheduled_trivia_generation``).

    Args:
        batch_size: Maximum number of wikis to consider in this run.

    Returns:
        ``{"wikis_considered": int, "questions_created": int}``.
    """
    from urbanlens.dashboard.models.wiki.model import Wiki

    already_generated_location_ids = TriviaQuestion.objects.filter(source=TriviaQuestionSource.AI_GENERATED).values_list("location_id", flat=True)
    candidates = (
        Wiki.objects.exclude(location_id__in=already_generated_location_ids)
        .exclude(description__isnull=True)
        .annotate(description_length=Length("description"))
        .filter(description_length__gte=MIN_DESCRIPTION_LENGTH)
        .select_related("location")
        .order_by("pk")[:batch_size]
    )

    wikis_considered = 0
    questions_created = 0
    for wiki in candidates:
        wikis_considered += 1
        questions_created += len(generate_questions_for_wiki(wiki))
    return {"wikis_considered": wikis_considered, "questions_created": questions_created}
