"""Presentation suggestions for a newly created label: an icon and a colour, from the first suggester that offers one."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.profile.model import Profile


@dataclass(frozen=True, slots=True)
class LabelStyleSuggestion:
    """Presentation fields suggested for a newly-created label."""

    icon: str | None = None
    color: str | None = None


class LabelStyleSuggester(ABC):
    """Chooses an icon and colour for a label being created."""

    @abstractmethod
    def suggest(self, name: str, profile: Profile) -> LabelStyleSuggestion | None:
        """
        Return a suggestion, or None to abstain, leaving the next suggester to decide.

        Args:
            name: The label name.
            profile: The profile creating the label.
        """


def suggest_label_style(name: str, profile: Profile) -> LabelStyleSuggestion:
    """Ask the suggesters in priority order; the first suggestion decides, and none leaves the label unstyled."""
    from urbanlens.dashboard.plugins.points import LABEL_STYLE_SUGGESTERS

    for suggester in LABEL_STYLE_SUGGESTERS.items():
        suggestion = suggester.suggest(name, profile)
        if suggestion is not None:
            return suggestion
    return LabelStyleSuggestion()


def resolve_or_create_styled_label(profile: Profile, name: str, kind: str) -> tuple[Label, bool]:
    """
    Resolve *name* as ``Label.objects.resolve_or_create`` does, giving a label it creates a suggested icon and colour.

    The suggestion is a model call, so only for callers off the request path.

    Args:
        profile: The owner of a created label.
        name: The label name.
        kind: The label kind.

    Returns:
        ``(label, created)``.

    Raises:
        ValueError: *name* is blank.
        CapacityExceededError: A label would be created, and the profile is at its limit.
    """
    from urbanlens.dashboard.models.labels.model import Label

    if (existing := Label.objects.named(profile, name, kind).first()) is not None:
        return existing, False
    style = suggest_label_style(name, profile)
    defaults = {field: value for field, value in (("icon", style.icon), ("color", style.color)) if value}
    return Label.objects.resolve_or_create(profile, name, kind, defaults=defaults)
