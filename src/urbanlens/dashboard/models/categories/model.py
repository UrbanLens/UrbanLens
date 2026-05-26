"""Category is now a kind of Tag. This module exists for backwards compatibility."""

from __future__ import annotations

from urbanlens.dashboard.models.tags.model import KIND_CATEGORY, Tag

# Category is no longer a separate model - it is a Tag with kind='category'.
Category = Tag

__all__ = ["Category", "KIND_CATEGORY"]  # noqa: RUF022
