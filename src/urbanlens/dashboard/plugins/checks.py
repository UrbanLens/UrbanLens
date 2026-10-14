"""System checks over the plugins this install found."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.core import checks
from django.core.checks import CheckMessage, register

from urbanlens.dashboard.plugins.registry import PluginState, plugin_registry

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.apps import AppConfig


@register()
def plugins_are_healthy(app_configs: Sequence[AppConfig] | None = None, **_kwargs: Any) -> list[CheckMessage]:
    """
    Warn about a plugin that could not start, and relay each active plugin's own checks.

    A disabled plugin is an operator's choice, so it is not reported.
    """
    messages: list[CheckMessage] = []
    for info in plugin_registry.plugins():
        if info.state in {PluginState.FAILED, PluginState.MISSING_REQUIREMENT}:
            messages.append(checks.Warning(f"Plugin {info.name!r} is inactive: {info.detail}.", id="urbanlens.plugins.W001", obj=info.source.module))
        elif info.is_active:
            try:
                messages.extend(info.plugin.check())
            except Exception as exc:
                messages.append(checks.Warning(f"Plugin {info.name!r} check() raised {exc!r}.", id="urbanlens.plugins.W002", obj=info.source.module))
    return messages
