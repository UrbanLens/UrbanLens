"""
Auto-tagging: choose labels for a pin or a wiki through the label matchers contributed to ``points.LABEL_MATCHERS``.

Each matcher runs in priority order on the eligible labels the earlier ones left, until enough are chosen. A wiki
has no owner, so it is offered only global labels and every matcher sees ``profile=None``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from urbanlens.dashboard.models.labels.model import Label
    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.models.profile.model import Profile
    from urbanlens.dashboard.models.wiki.model import Wiki

logger = logging.getLogger(__name__)


class AutoTagService:
    """
    Suggests and optionally applies labels to a pin or a wiki.

    Args:
        kinds: Label kinds to process.
        max_labels: Maximum suggestions per kind; None for no limit.
    """

    _DEFAULT_KINDS: tuple[str, ...] = ("category",)

    def __init__(self, kinds: list[str] | tuple[str, ...] | None = None, max_labels: int | None = None) -> None:
        self.kinds = list(kinds) if kinds is not None else list(self._DEFAULT_KINDS)
        self.max_labels = max_labels

    def suggest_for_pin(self, pin: Pin, *, apply: bool = False) -> list[Label]:
        """
        Suggest, and optionally apply, labels for a pin from the global labels and its owner's own.

        Returns:
            The chosen labels across every configured kind.
        """
        profile = getattr(pin, "profile", None)
        return self._suggest(pin, profile, apply=apply)

    def suggest_for_wiki(self, wiki: Wiki, *, apply: bool = False) -> list[Label]:
        """
        Suggest, and optionally apply, global labels for a community wiki.

        Returns:
            The chosen labels across every configured kind.
        """
        return self._suggest(wiki, None, apply=apply)

    def _suggest(self, target: Pin | Wiki, profile: Profile | None, *, apply: bool) -> list[Label]:
        results: list[Label] = []
        for kind in self.kinds:
            matched = self.match(target, self._eligible_labels(kind, profile=profile), kind, profile)
            if apply and matched:
                to_apply = self._exclude_removed(target, matched)
                if to_apply:
                    target.labels.add(*to_apply)
            results.extend(matched)
        return results

    def match(self, target: Pin | Wiki, eligible: list[Label], kind: str, profile: Profile | None) -> list[Label]:
        """
        Run every applicable matcher in priority order on the labels the earlier ones left.

        A matcher that raises is logged and skipped, so one unavailable service never stops the others.

        Returns:
            Up to ``max_labels`` chosen labels, in the order they were chosen.
        """
        from urbanlens.dashboard.plugins.points import LABEL_MATCHERS

        matched: list[Label] = []
        for matcher in LABEL_MATCHERS:
            remaining = [label for label in eligible if label not in matched]
            wanted = None if self.max_labels is None else self.max_labels - len(matched)
            if not remaining or wanted == 0:
                break
            if not matcher.applies(target, kind, profile):
                continue
            try:
                chosen = matcher.match(target, remaining, kind, limit=wanted)
            except Exception:
                logger.exception("Label matcher %r failed for %s %s", matcher.slug, type(target).__name__, target.pk)
                continue
            allowed = {label.pk for label in remaining}
            matched.extend(label for label in dict.fromkeys(chosen) if label.pk in allowed)
        return matched if self.max_labels is None else matched[: self.max_labels]

    @staticmethod
    def _exclude_removed(target: Pin | Wiki, matched: list[Label]) -> list[Label]:
        """``matched`` without any label the user already removed from this target, which is never re-applied."""
        from urbanlens.dashboard.models.auto_removals.model import AutoRemovalKind, PinAutoRemoval, WikiAutoRemoval

        if type(target).__name__ == "Wiki":
            removed_ids = set(WikiAutoRemoval.objects.filter(wiki=target, kind=AutoRemovalKind.LABEL).values_list("value", flat=True))
        else:
            removed_ids = set(PinAutoRemoval.objects.filter(pin=target, kind=AutoRemovalKind.LABEL).values_list("value", flat=True))
        return [label for label in matched if str(label.pk) not in removed_ids]

    @staticmethod
    def _eligible_labels(kind: str, *, profile: Profile | None) -> list[Label]:
        """Labels of ``kind`` open to auto-tagging: global ones, plus the owner's own for a pin."""
        from django.db.models import Q

        from urbanlens.dashboard.models.labels.model import Label

        qs = Label.objects.filter(kind=kind, allow_auto_tag=True).exclude(is_protected=True)
        qs = qs.filter(Q(profile__isnull=True) | Q(profile=profile)) if profile is not None else qs.filter(profile__isnull=True)
        return list(qs.order_by("name"))
