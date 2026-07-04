# Generic imports
from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class Importance(TextChoices):
    """
    Choices used for recording the status of a notification.

    This is used as a class, and never instantiated.
    """

    LOWEST = "lowest", "Lowest"
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    HIGHEST = "highest", "Highest"
