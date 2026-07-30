"""Content classifier for Trivia questions - shared by the user-submission and AI-generation paths.

Per the Trivia spec: both a user-submitted question and an AI-generated one
must be judged by the exact same classifier, using the exact same rules.
This is the highest-harm piece of the whole feature - a false negative lets a
bullying/in-group question reach real users with no report path that traces
back to "the filter missed this," and a false positive silently kills a
legitimate question with (by design) no feedback loop for the author to
notice or correct. Every failure mode here defaults to REJECT (fail closed).

Follows ``services.auto_tag``'s allowlisted-``<ANSWER>`` pattern: the model
must answer with exactly one token from a fixed set; anything else
(unparseable, empty, gateway unavailable) is treated as a rejection, never
as an approval.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.scanner import wrap_user_data
from urbanlens.dashboard.services.rate_limiter import log_api_call

if TYPE_CHECKING:
    from urbanlens.dashboard.models.location.model import Location
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

#: The only tokens the classifier is allowed to answer with. Anything else -
#: including no response at all - is treated as REJECT_UNPARSEABLE, never as
#: an approval. Keep in sync with _INSTRUCTIONS below.
_REJECT_TOKENS = {"REJECT_PERSON", "REJECT_BULLYING", "REJECT_INGROUP", "REJECT_OFF_TOPIC"}
_ALLOWED_TOKENS = {"APPROVE", *_REJECT_TOKENS}

_INSTRUCTIONS = """You are a content moderator for a trivia game about real-world locations (mostly abandoned or historic buildings explored by an urban-exploration community). You will be shown one proposed trivia question and its accepted answer. Decide whether it is safe to add to the public question pool.

Approve a question ONLY if it is about the location itself - its history, architecture, construction dates, ownership, structural facts, geography, or similar topics.

Reject a question in ANY of these cases:
1. PERSON - it is about, or centers on, a specific individual, even if only referenced indirectly and never named. A question phrased like "the year someone did X" or "the person who explored here" still centers a person and must be rejected, because members of a small community will very likely know exactly who is meant even without a name.
2. BULLYING - it includes an insulting, mocking, or bullying adjective or description, even if no specific target is named.
3. INGROUP - it references a specific group of people, crew, team, or "party" (e.g. an exploring group's visit), rather than the location itself. This kind of knowledge is only accessible to insiders, not the general trivia audience.
4. OFF_TOPIC - it is not actually about a location, its history, or its architecture at all.

When in doubt, reject - the cost of a missed good question is much lower than the cost of a question that embarrasses or excludes people.

Examples:
Question: "What was the building's original use in 1920?" Answer: "A textile mill" -> APPROVE (purely about the location)
Question: "What was X in the year somebody did Y?" -> REJECT_PERSON (implies a specific person via "somebody," even though unnamed)
Question: "When was the first party at X?" -> REJECT_INGROUP (about a specific exploring group's history, not the location)
Question: "How many stories tall is the main building?" Answer: "5" -> APPROVE (purely structural)

Respond with EXACTLY ONE of the following tokens, wrapped in ANSWER tags, and nothing else:
<ANSWER>APPROVE</ANSWER>
<ANSWER>REJECT_PERSON</ANSWER>
<ANSWER>REJECT_BULLYING</ANSWER>
<ANSWER>REJECT_INGROUP</ANSWER>
<ANSWER>REJECT_OFF_TOPIC</ANSWER>"""


@dataclass(frozen=True)
class ClassifierVerdict:
    """The classifier's decision. ``reason`` is None only when ``approved`` is True."""

    approved: bool
    reason: str | None = None


def _build_prompt(prompt: str, answer: str, location: Location) -> str:
    """Build the classification prompt, wrapping untrusted text against injection."""
    location_name = location.official_name or "Unknown location"
    return f"Location: {location_name}\nQuestion: {wrap_user_data(prompt)}\nAccepted answer: {wrap_user_data(answer)}"


def classify_trivia_question(prompt: str, answer: str, location: Location, *, profile: Profile | None = None) -> ClassifierVerdict:
    """Judge whether a trivia question is safe to add to the public rotation.

    Used identically for a user-submitted question (before it leaves
    PENDING_REVIEW) and for an AI-generated one (before it is ever
    persisted) - see this module's docstring for why both paths must share
    the exact same rules.

    Args:
        prompt: The question text.
        answer: The canonical accepted answer.
        location: The location the question is about.
        profile: The submitting profile, if user-submitted (used only for
            the AI-availability gate - AI generation has no submitting
            profile and passes None).

    Returns:
        APPROVE, or REJECT with a reason - AI unavailable, an unparseable
        response, or one of the classifier's own reject categories.
    """
    gateway = get_gateway("trivia_moderation", profile=profile, instructions=_INSTRUCTIONS)
    if gateway is None:
        logger.info("Trivia classifier unavailable (AI disabled); rejecting fail-closed")
        return ClassifierVerdict(approved=False, reason="ai_unavailable")

    user_prompt = _build_prompt(prompt, answer, location)

    started = time.monotonic()
    try:
        raw = gateway.send_prompt(user_prompt)
    except Exception:
        # A transport-level failure (provider outage, DNS, an unrecognized
        # model tripping the token-counting library, etc.) must never bubble
        # up and 500 the submitter's request - it's just another form of
        # "AI unavailable right now," same as a None response.
        logger.exception("Trivia classifier call failed unexpectedly; rejecting fail-closed")
        log_api_call("trivia_moderation", success=False)
        return ClassifierVerdict(approved=False, reason="ai_unavailable")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log_api_call("trivia_moderation", success=raw is not None, response_ms=elapsed_ms, endpoint=gateway.model, cost_estimate=gateway.cost)

    if raw is None:
        logger.warning("Trivia classifier got no response from the AI gateway; rejecting fail-closed")
        return ClassifierVerdict(approved=False, reason="ai_unavailable")

    verdict_word = raw.strip().upper()
    if verdict_word == "APPROVE":
        return ClassifierVerdict(approved=True)
    if verdict_word in _REJECT_TOKENS:
        return ClassifierVerdict(approved=False, reason=verdict_word.removeprefix("REJECT_").lower())

    logger.warning("Trivia classifier returned an unrecognized token %r; rejecting fail-closed", verdict_word)
    return ClassifierVerdict(approved=False, reason="unparseable")
