"""Category is now a kind of Badge. This module exists for backwards compatibility."""

from __future__ import annotations

from urbanlens.dashboard.models.badges.model import KIND_CATEGORY, Badge

# Category is no longer a separate model - it is a Badge with kind='category'.
Category = Badge

__all__ = ["Category", "KIND_CATEGORY"]  # noqa: RUF022
