"""AI fallback for a Trivia answer that doesn't exact-match the canonical one.
Only ever consulted on a normalized-string mismatch (``services.trivia.session.submit_answer``) - an exact match never reaches this module."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.scanner import wrap_user_data
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError, api_call_slot

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_INSTRUCTIONS = """You judge whether a trivia player's answer means the same thing as the accepted answer, just phrased differently (different capitalization, spelling out vs. abbreviating, a nickname, extra or missing words that don't change the meaning, etc.) - NOT whether it's merely related or partially correct.

Respond with EXACTLY ONE of the following tokens, wrapped in ANSWER tags, and nothing else:
<ANSWER>MATCH</ANSWER>
<ANSWER>NO_MATCH</ANSWER>"""


def is_answer_equivalent(raw_answer: str, accepted_answer: str, *, profile: Profile) -> bool:
    """Ask AI whether ``raw_answer`` means the same thing as ``accepted_answer``, differently phrased.
    Only called after a normalized-string mismatch already ruled out an exact match.

    Args:
        raw_answer: What the player typed, as typed.
        accepted_answer: The question's canonical accepted answer.
        profile: The answering profile - used for the AI subscription-feature gate.

    Returns:
        True only if the AI explicitly judges the two equivalent."""
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if not user_has_feature(profile.user, SiteFeature.AI):
        return False

    gateway = get_gateway("trivia_answer_check", profile=profile, instructions=_INSTRUCTIONS)
    if gateway is None:
        return False

    prompt = f"Accepted answer: {wrap_user_data(accepted_answer)}\nPlayer's answer: {wrap_user_data(raw_answer)}"

    try:
        with api_call_slot("trivia_answer_check", endpoint=gateway.model) as slot:
            try:
                raw = gateway.send_prompt(prompt)
            except Exception:
                # A transport-level failure (provider outage, DNS, an unrecognized model tripping the
                # token-counting library, etc.) must never bubble up and 500 the player's answer submission
                # - it's just another form of "AI unavailable right now," same as a None response.
                logger.exception("Trivia answer-check call failed unexpectedly; treating as no match")
                return False
            slot.success, slot.cost_estimate = raw is not None, gateway.cost
    except RequestCancelledError:
        logger.info("trivia_answer_check refused by its rate limit or switch")
        return False

    if raw is None:
        logger.info("Trivia answer-check got no response from the AI gateway; treating as no match")
        return False
    return raw.strip().upper() == "MATCH"
