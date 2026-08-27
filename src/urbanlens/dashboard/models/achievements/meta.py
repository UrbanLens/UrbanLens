# Generic imports
from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class ActivityKind(TextChoices):
    """The daily actions a streak can be built from.

    Each value is one row per profile per calendar day in
    :class:`~urbanlens.dashboard.models.achievements.model.ProfileActivityDay`,
    which is what makes streaks idempotent: uploading thirty photos in one day
    is still a single day of the "photos" streak.
    """

    LOGIN = "login", "Logged in"
    PHOTO = "photo", "Uploaded a photo"
    WIKI_EDIT = "wiki_edit", "Edited a wiki"
    PIN = "pin", "Pinned a spot"
    COMMENT = "comment", "Left a comment"


#: Metric keys are declared by the metric registry in
#: ``services.achievements.metrics`` rather than here, because a metric is a
#: query over other models and the registry is the only place that knows how to
#: run one. These constants exist so signal handlers and the streak recorder can
#: name a metric without importing the registry.
METRIC_STREAK_PREFIX = "streak_"


def streak_metric_key(kind: str) -> str:
    """Return the metric key that reads the longest streak for *kind*.

    Args:
        kind: An :class:`ActivityKind` value.

    Returns:
        The registry key of the matching streak metric.
    """
    return f"{METRIC_STREAK_PREFIX}{kind}"
