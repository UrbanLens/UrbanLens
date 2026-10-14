"""The setup checklist and the site-admin pages."""

from __future__ import annotations

from typing import Any

from urbanlens.dashboard.plugins.extensions import FilterHook

SETUP_FEATURE_GROUPS: FilterHook[list[dict[str, Any]], []] = FilterHook(
    "setup.feature_groups",
    'The groups of the setup page\'s integrations checklist, each ``{"label", "icon", "items": [{"name", "description", "env_var", "configured"}]}``. A plugin adds the groups for the integrations it provides.',
)

__all__ = ["SETUP_FEATURE_GROUPS"]
