"""Helpers for testing plugins and the seams they contribute to."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from urbanlens.dashboard.plugins.registry import plugin_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from urbanlens.dashboard.plugins.base import UrbanLensPlugin
    from urbanlens.dashboard.plugins.extensions import Seam


def contributed[T](owner: str | None, seam: Seam[T]) -> list[T]:
    """What one contributor (a plugin name, or None for core) added to ``seam``, in priority order."""
    return [contribution.value for contribution in seam.contributions() if contribution.owner == owner]


@contextmanager
def plugin_active(plugin: UrbanLensPlugin | type[UrbanLensPlugin]) -> Iterator[UrbanLensPlugin]:
    """
    Register and set up ``plugin`` for the block, then remove it and withdraw what it contributed.

    Raises:
        ValueError: When the plugin's name is already registered (an installed plugin is active already).
    """
    info = plugin_registry.register(plugin)
    if info is None:
        raise ValueError(f"Could not register {plugin!r}; is a plugin of that name already registered?")
    try:
        yield info.plugin
    finally:
        plugin_registry.unregister(info.name)


@contextmanager
def plugin_withdrawn(name: str) -> Iterator[None]:
    """
    Withdraw an installed plugin's contributions for the block, so a test sees core as an install without it would.

    Its tables stay, as they would for a disabled plugin. The contributions are restored by setting it up again.
    Routes are the exception: the URLconf reads ``ROOT_URLS`` once, at import, so it is loaded here first rather
    than built without the plugin for the rest of the process. A test that needs the routes gone patches reverse.
    """
    from django.urls import get_resolver

    from urbanlens.dashboard.plugins.base import PluginContext
    from urbanlens.dashboard.plugins.extensions import discard_contributions

    info = plugin_registry.info(name)
    if info is None or not info.is_active:
        yield
        return
    get_resolver().url_patterns  # noqa: B018 - imports the URLconf while the plugin's routes are still contributed
    discard_contributions(owner=name)
    try:
        yield
    finally:
        info.plugin.setup(PluginContext(info.plugin))


def seam_values(seam: Seam[Any]) -> list[Any]:
    """Every value contributed to ``seam``, whoever contributed it."""
    return [contribution.value for contribution in seam.contributions()]
