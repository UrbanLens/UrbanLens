from __future__ import annotations

from urbanlens.dashboard.models.abstract.choices import TextChoices


class Importance(TextChoices):
    """Choices for notification importance."""

    LOWEST = "lowest", "Lowest"
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    HIGHEST = "highest", "Highest"
