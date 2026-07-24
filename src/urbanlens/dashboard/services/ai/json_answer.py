"""Tolerant JSON parsing for the app's "respond with one JSON object" LLM protocol."""

from __future__ import annotations

import json


def parse_json_answer(answer: str) -> dict | None:
    """Parse one JSON object out of a model's answer.

    Tries a clean parse first, then falls back to slicing between the first
    ``{`` and last ``}`` to tolerate a model that wrapped the object in a
    sentence despite instructions not to.

    Args:
        answer: The raw text returned by the model.

    Returns:
        The parsed dict, or None if no JSON object could be recovered.
    """
    answer = answer.strip()
    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        start, end = answer.find("{"), answer.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(answer[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None
