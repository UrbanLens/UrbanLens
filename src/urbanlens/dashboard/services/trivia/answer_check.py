"""AI fallback for a Trivia answer that doesn't exact-match the canonical one.

Only ever consulted on a normalized-string mismatch
(``services.trivia.session.submit_answer``) - an exact match never reaches
this module. Gated on ``SiteFeature.AI``; a profile without that
subscription feature always falls back to exact-match-only, never blocked
from playing.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.scanner import wrap_user_data
from urbanlens.dashboard.services.core.rate_limiter import log_api_call

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

logger = logging.getLogger(__name__)

_INSTRUCTIONS = """You judge whether a trivia player's answer means the same thing as the accepted answer, just phrased differently (different capitalization, spelling out vs. abbreviating, a nickname, extra or missing words that don't change the meaning, etc.) - NOT whether it's merely related or partially correct.

Respond with EXACTLY ONE of the following tokens, wrapped in ANSWER tags, and nothing else:
<ANSWER>MATCH</ANSWER>
<ANSWER>NO_MATCH</ANSWER>"""


def is_answer_equivalent(raw_answer: str, accepted_answer: str, *, profile: Profile) -> bool:
    """Ask AI whether ``raw_answer`` means the same thing as ``accepted_answer``, differently phrased.

    Only called after a normalized-string mismatch already ruled out an
    exact match. Fails closed to "not equivalent" (the answer counts as
    wrong) on any AI unavailability or unparseable response - it never
    upgrades a wrong answer to correct just because the AI call itself
    failed.

    Args:
        raw_answer: What the player typed, as typed.
        accepted_answer: The question's canonical accepted answer.
        profile: The answering profile - used for the AI subscription-feature gate.

    Returns:
        True only if the AI explicitly judges the two equivalent.
    """
    from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

    if not user_has_feature(profile.user, SiteFeature.AI):
        return False

    gateway = get_gateway("trivia_answer_check", profile=profile, instructions=_INSTRUCTIONS)
    if gateway is None:
        return False

    prompt = f"Accepted answer: {wrap_user_data(accepted_answer)}\nPlayer's answer: {wrap_user_data(raw_answer)}"

    started = time.monotonic()
    try:
        raw = gateway.send_prompt(prompt)
    except Exception:
        # A transport-level failure (provider outage, DNS, an unrecognized
        # model tripping the token-counting library, etc.) must never bubble
        # up and 500 the player's answer submission - it's just another form
        # of "AI unavailable right now," same as a None response.
        logger.exception("Trivia answer-check call failed unexpectedly; treating as no match")
        log_api_call("trivia_answer_check", success=False)
        return False
    elapsed_ms = int((time.monotonic() - started) * 1000)
    log_api_call("trivia_answer_check", success=raw is not None, response_ms=elapsed_ms, endpoint=gateway.model, cost_estimate=gateway.cost)

    if raw is None:
        logger.info("Trivia answer-check got no response from the AI gateway; treating as no match")
        return False
    return raw.strip().upper() == "MATCH"
