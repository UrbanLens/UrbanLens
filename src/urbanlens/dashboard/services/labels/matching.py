"""
Label matchers: the stages auto-tagging runs to choose labels for a pin or a wiki.

Auto-tagging asks every matcher contributed to ``points.LABEL_MATCHERS`` in priority order, each choosing from the
eligible labels the earlier ones left. Core contributes :class:`KeywordLabelMatcher`, which needs no external service;
plugins contribute matchers backed by their own services.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    import re

    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)

#: Maps label kind to the Profile field that enables keyword-based auto-tagging for that kind.
KEYWORD_KIND_PREFERENCE: dict[str, str] = {
    "category": "keyword_label_categories",
    "tag": "keyword_label_tags",
    "status": "keyword_label_statuses",
}


class LabelMatcher(ABC):
    """
    One auto-tagging stage.

    Attributes:
        slug: Identifies the stage in logs.
    """

    slug: ClassVar[str] = ""

    @abstractmethod
    def applies(self, target: Pin | Wiki, kind: str, profile: Profile | None) -> bool:
        """
        Whether this stage runs for ``target``.

        Args:
            target: The pin or wiki being tagged.
            kind: The label kind being chosen.
            profile: The pin's owner; None for a wiki, which has none.
        """

    @abstractmethod
    def match(self, target: Pin | Wiki, eligible: list[Label], kind: str, *, limit: int | None) -> list[Label]:
        """
        Choose labels for ``target``.

        Args:
            target: The pin or wiki being tagged.
            eligible: The labels this stage may choose from; never empty.
            kind: Their kind.
            limit: At most this many are wanted; None for no limit.

        Returns:
            Chosen labels, each one of ``eligible``, best first.
        """


class KeywordLabelMatcher(LabelMatcher):
    """Match label names and each label's own keywords against the target's name. No external call."""

    slug = "keywords"

    def applies(self, target: Pin | Wiki, kind: str, profile: Profile | None) -> bool:
        if profile is None:
            return True
        if not getattr(profile, "keyword_tagging_enabled", True):
            return False
        preference = KEYWORD_KIND_PREFERENCE.get(kind)
        return bool(getattr(profile, preference, True)) if preference else True

    def match(self, target: Pin | Wiki, eligible: list[Label], kind: str, *, limit: int | None) -> list[Label]:
        from urbanlens.dashboard.services.labels.keyword_patterns import _get_compiled

        text = self.keyword_text(target)
        if not text:
            return []
        compiled = _get_compiled()
        matched: list[Label] = []
        for label in eligible:
            if self.label_matches_text(label, text, compiled):
                matched.append(label)
                if limit and len(matched) >= limit:
                    break
        return matched

    @staticmethod
    def keyword_text(target: Pin | Wiki) -> str:
        """
        The text keyword matching reads: the meaningful official name, and a wiki's community name.

        Addresses are left out, so "Church Street" does not match the Church category. A pin's own name is a personal
        label and is left out too.
        """
        from urbanlens.dashboard.services.locations.naming import is_meaningful_name

        parts: list[str] = []
        name = getattr(target, "meaningful_official_name", None)
        if name is None:
            raw = getattr(target, "official_name", None)
            if raw and is_meaningful_name(raw):
                name = raw
        if name:
            parts.append(name)
        if type(target).__name__ == "Wiki":
            community_name = getattr(target, "name", None)
            if community_name and is_meaningful_name(community_name) and community_name not in parts:
                parts.append(community_name)
        return " ".join(parts)

    @staticmethod
    def label_matches_text(label: Label, text: str, compiled_patterns: dict[str, list[re.Pattern[str]]]) -> bool:
        """Whether the label's built-in name patterns, or any of its own comma-separated keywords, occur in ``text``."""
        label_name = label.name.lower()
        for category, patterns in compiled_patterns.items():
            if category.lower() == label_name and any(pattern.search(text) for pattern in patterns):
                return True
        if label.keywords:
            lower_text = text.lower()
            return any(needle and needle in lower_text for needle in (keyword.strip().lower() for keyword in label.keywords.split(",")))
        return False
