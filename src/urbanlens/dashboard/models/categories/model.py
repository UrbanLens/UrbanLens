"""Category is now a kind of Label. This module exists for backwards compatibility."""

from __future__ import annotations

from urbanlens.dashboard.models.labels.model import KIND_CATEGORY, Label

# Category is no longer a separate model - it is a Label with kind='category'.
Category = Label

__all__ = ["Category", "KIND_CATEGORY"]  # noqa: RUF022
