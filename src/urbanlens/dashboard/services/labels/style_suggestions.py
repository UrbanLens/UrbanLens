"""AI-assisted label presentation suggestions."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING, Any, cast

from django.contrib.auth.models import AnonymousUser

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser

    from urbanlens.dashboard.models.profile.model import Profile

from urbanlens.dashboard.models.labels.meta import COLOR_CHOICES, ICON_CATEGORIES
from urbanlens.dashboard.models.subscriptions import SiteFeature, user_has_feature

logger = logging.getLogger(__name__)

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")


@dataclass(frozen=True, slots=True)
class LabelStyleSuggestion:
    """Presentation fields suggested for a newly-created label."""

    icon: str | None = None
    color: str | None = None


def suggest_label_style(name: str, profile: Profile) -> LabelStyleSuggestion:
    """Ask AI to choose an emoji and color for a label when the user may use AI.

    The suggestion is best-effort: callers can safely fall back to the default label
    appearance when subscription, profile preference, site settings, or the AI gateway
    prevents a suggestion.
    """
    if not user_has_feature(profile.user, SiteFeature.AI) or not profile.ai_enabled or not profile.external_apis_enabled:
        return LabelStyleSuggestion()

    prompt = _build_prompt(name)
    if not prompt:
        return LabelStyleSuggestion()

    from urbanlens.dashboard.services.ai.factory import get_gateway

    try:
        gateway = get_gateway("label_style_suggestions", profile=profile, instructions=_build_instructions())
    except (RuntimeError, ValueError, OSError) as exc:
        logger.warning("AI gateway unavailable for label style suggestion %r: %s", name, exc)
        return LabelStyleSuggestion()
    if not gateway:
        return LabelStyleSuggestion()

    try:
        answers = gateway.send_prompt_list(prompt, max_results=2)
    except (RuntimeError, ValueError, OSError) as exc:
        logger.warning("AI label style suggestion failed for %r: %s", name, exc)
        return LabelStyleSuggestion()

    return _parse_answers(answers)


def _build_prompt(name: str) -> str:
    from urbanlens.dashboard.services.ai.scanner import wrap_user_data

    clean_name = (name or "").strip()[:255]
    if not clean_name:
        return ""
    return "Label name:\n" + wrap_user_data(clean_name)


def _build_instructions() -> str:
    colors = ", ".join(color for color, _label in COLOR_CHOICES)
    emojis = ", ".join(_emoji_options())
    return (
        "Choose a clear visual style for a newly-created Urban Lens pin-import label.\n"
        "Return exactly two ANSWER tags: first one emoji, then one hex color.\n"
        f"The emoji MUST be one of these options: {emojis}.\n"
        f"The color MUST be one of these options: {colors}.\n"
        "Do not return explanations or any values outside those lists."
    )


def _emoji_options() -> list[str]:
    seen: set[str] = set()
    options: list[str] = []
    for _label, icons in ICON_CATEGORIES.values():
        for icon, _icon_label in icons:
            if icon not in seen:
                seen.add(icon)
                options.append(icon)
    return options


def _parse_answers(answers: list[str]) -> LabelStyleSuggestion:
    valid_emojis = set(_emoji_options())
    valid_colors = {color.upper(): color for color, _label in COLOR_CHOICES}
    icon: str | None = None
    color: str | None = None

    for raw in answers:
        value = raw.strip()
        if icon is None and value in valid_emojis:
            icon = value
            continue
        if color is None:
            match = _HEX_RE.search(value)
            if match:
                color = valid_colors.get(match.group(0).upper())

    return LabelStyleSuggestion(icon=icon, color=color)
