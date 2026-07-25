"""AI wiki-incorporation for well-upvoted user-submitted Trivia questions.

Once a ``USER_SUBMITTED``, ``APPROVED`` question's community vote score
crosses :data:`WIKI_INCORPORATION_SCORE_THRESHOLD`, this module drafts a new
plain-text paragraph folding the trivia fact into the location's wiki
article - reusing the exact same draft -> sanitize -> safety-classify ->
append pipeline as :mod:`services.ai.article_expansion` (same
``sanitize_article_plain_text``, same ``article_safety.classify_article_text``,
same ``article_expansion.append_to_article``), with only the writing step
itself carrying a dedicated AI feature (``trivia_wiki_incorporation``) so it
has its own SiteSettings toggle and cost tracking. The safety bar for what
belongs in a wiki article does not depend on where the source material came
from, so that classifier is shared outright rather than duplicated.

``TriviaQuestion.wiki_incorporated_at`` is set once a question has been fully
processed - approved-and-appended, safety-rejected, or nothing new to say -
so it is never proposed to the writing model a second time. An AI-unavailable
outcome deliberately leaves it unset so a later sweep retries, mirroring
``services.trivia.submission.classify_and_update``'s "never mass-finalize
during an outage" rule.
"""

from __future__ import annotations

import logging
import time

from django.utils import timezone

from urbanlens.dashboard.models.trivia.model import TriviaQuestion, TriviaQuestionSource, TriviaQuestionStatus
from urbanlens.dashboard.services.ai.article_expansion import append_to_article, sanitize_article_plain_text
from urbanlens.dashboard.services.ai.article_safety import classify_article_text
from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.scanner import wrap_user_data
from urbanlens.dashboard.services.articles import get_article
from urbanlens.dashboard.services.rate_limiter import log_api_call
from urbanlens.dashboard.services.trivia.voting import effective_score

logger = logging.getLogger(__name__)

#: Net weighted vote score (see services.trivia.voting's weights) a
#: USER_SUBMITTED question must reach before it's considered for wiki
#: incorporation - "to-be-tuned" per the Trivia spec; set well above the
#: eligibility.py rotation gate (>= 0) so only genuinely well-received
#: questions, not merely non-negative ones, ever reach the wiki.
WIKI_INCORPORATION_SCORE_THRESHOLD = 5.0

#: How many not-yet-processed candidate questions one sweep considers.
DEFAULT_SWEEP_BATCH_SIZE = 25

#: Match article_expansion's page-text budget so writing stays within token limits.
MAX_EXISTING_ARTICLE_CHARS = 12_000

EDIT_SUMMARY_INCORPORATED_TRIVIA = "Incorporated a well-upvoted trivia question"

_WRITING_INSTRUCTIONS = """You expand encyclopedia-style articles about physical places for an urban-exploration \
mapping community. You will be given one trivia fact (a question and its accepted answer) that the \
community has upvoted, plus the location's existing wiki article. Write ONE short new plain-text \
paragraph that incorporates the fact into the article's prose - do not just restate the question and \
answer verbatim.

Rules:
- Output plain text only: no HTML, Markdown, bullet markers, headings, links, images, JSON, or code fences.
- Do not invent facts beyond what the trivia fact and existing article already establish.
- Do not include entry methods, trespass advice, security-bypass details, or other operational access guidance.
- Do not include personal attacks, doxxing, or inappropriate content.
- If the fact is already covered by the existing article, respond with an empty ANSWER.

Wrap the paragraph (or nothing) in ANSWER tags:
<ANSWER>
paragraph text
</ANSWER>"""


def _slice_existing(content: str) -> str:
    """Bound existing article text handed to the writing model."""
    content = (content or "").strip()
    if len(content) <= MAX_EXISTING_ARTICLE_CHARS:
        return content
    return content[:MAX_EXISTING_ARTICLE_CHARS] + "\n…"


def _draft_paragraph(*, place_name: str, prompt: str, answer: str, existing_article: str) -> str | None:
    """Call the writing gateway; return the raw answer text, or None on failure/unavailability."""
    gateway = get_gateway("trivia_wiki_incorporation", instructions=_WRITING_INSTRUCTIONS)
    if gateway is None:
        return None

    full_prompt = "\n".join(
        [
            f"The place is known as: {place_name!r}.",
            "",
            "Trivia fact (community-upvoted):",
            wrap_user_data(f"Q: {prompt}\nA: {answer}"),
            "",
            "Existing wiki article:",
            wrap_user_data(_slice_existing(existing_article) or "(empty)"),
        ],
    )

    started = time.monotonic()
    try:
        answer_text = gateway.send_prompt(full_prompt)
    except Exception:
        logger.exception("Trivia wiki-incorporation writing call failed")
        log_api_call("trivia_wiki_incorporation", success=False)
        return None
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log_api_call(
        "trivia_wiki_incorporation",
        success=answer_text is not None,
        response_ms=elapsed_ms,
        endpoint=gateway.model,
        cost_estimate=gateway.cost,
    )
    return answer_text


def _mark_processed(question: TriviaQuestion) -> None:
    """Stamp a question as fully handled so it's never proposed to the writing model again."""
    question.wiki_incorporated_at = timezone.now()
    question.save(update_fields=["wiki_incorporated_at", "updated"])


def incorporate_question_into_wiki(question: TriviaQuestion) -> bool:
    """Draft, sanitize, safety-check, and append one well-upvoted question's fact to its wiki.

    Skipped without any AI call when the question is already processed, isn't
    a still-approved ``USER_SUBMITTED`` question, has no location wiki, or
    hasn't crossed :data:`WIKI_INCORPORATION_SCORE_THRESHOLD`. Never raises -
    an unexpected failure anywhere in the pipeline is logged and treated as a
    skip, so one bad question can't abort a whole sweep batch.

    Args:
        question: The candidate question. Callers processing a batch should
            ``select_related("location", "location__wiki")`` to avoid N+1s.

    Returns:
        True only when new text was actually appended to the wiki article.
        False covers every skip reason, including "processed but nothing was
        added" (safety-rejected, or nothing new to say) - callers that need
        to distinguish those should inspect ``question.wiki_incorporated_at``
        afterward.
    """
    try:
        if question.wiki_incorporated_at is not None:
            return False
        if question.source != TriviaQuestionSource.USER_SUBMITTED or question.status != TriviaQuestionStatus.APPROVED:
            return False
        wiki = getattr(question.location, "wiki", None)
        if wiki is None:
            return False
        if effective_score(question) < WIKI_INCORPORATION_SCORE_THRESHOLD:
            return False

        place_name = question.location.official_name or "Unknown location"
        article = get_article(wiki=wiki)
        existing = article.content if article else ""

        raw = _draft_paragraph(place_name=place_name, prompt=question.prompt, answer=question.answer, existing_article=existing)
        if raw is None:
            # AI unavailable - leave wiki_incorporated_at unset so a later
            # sweep retries, rather than silently losing this question.
            return False

        new_text = sanitize_article_plain_text(raw)
        if not new_text:
            _mark_processed(question)
            return False

        verdict = classify_article_text(new_text, place_name=place_name)
        if not verdict.approved:
            _mark_processed(question)
            return False

        applied, _note = append_to_article(
            editor=None,
            existing=existing,
            new_text=new_text,
            wiki=wiki,
            edit_summary=EDIT_SUMMARY_INCORPORATED_TRIVIA,
        )
        _mark_processed(question)
        return applied
    except Exception:
        logger.exception("Trivia wiki incorporation failed unexpectedly for question %s", question.pk)
        return False


def sweep_questions_for_wiki_incorporation(*, batch_size: int = DEFAULT_SWEEP_BATCH_SIZE) -> dict[str, int]:
    """Consider a bounded batch of not-yet-processed, well-upvoted questions for wiki incorporation.

    Called from a scheduled Celery task (``tasks.run_scheduled_trivia_wiki_incorporation``).

    Args:
        batch_size: Maximum number of candidate questions to consider in this run.

    Returns:
        ``{"questions_considered": int, "questions_incorporated": int}``.
    """
    candidates = (
        TriviaQuestion.objects.filter(
            source=TriviaQuestionSource.USER_SUBMITTED,
            status=TriviaQuestionStatus.APPROVED,
            wiki_incorporated_at__isnull=True,
            location__wiki__isnull=False,
        )
        .select_related("location", "location__wiki")
        .order_by("pk")[:batch_size]
    )

    questions_considered = 0
    questions_incorporated = 0
    for question in candidates:
        questions_considered += 1
        if incorporate_question_into_wiki(question):
            questions_incorporated += 1
    return {"questions_considered": questions_considered, "questions_incorporated": questions_incorporated}
