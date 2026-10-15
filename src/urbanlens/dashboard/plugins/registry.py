"""
The plugins this install found, and their lifecycle once Django is ready.

:mod:`~urbanlens.dashboard.plugins.loader` finds plugins while settings load; :meth:`PluginRegistry.discover` (run
from the dashboard app's ``ready()``) imports each one, resolves what it requires, and calls ``setup()`` on the
active ones. A plugin that fails to import, instantiate or set up is logged and left inactive - one broken plugin
never prevents startup - and whatever it contributed before failing is withdrawn.
"""

from __future__ import annotations

from dataclasses import dataclass
import enum
import importlib
import inspect
import logging

from urbanlens.dashboard.plugins.base import PluginContext, UrbanLensPlugin
from urbanlens.dashboard.plugins.extensions import discard_contributions
from urbanlens.dashboard.plugins.loader import PluginSource, find_plugin_sources

logger = logging.getLogger(__name__)


class PluginState(enum.StrEnum):
    """Where a discovered plugin stands."""

    ACTIVE = "active"
    DISABLED = "disabled"
    MISSING_REQUIREMENT = "missing requirement"
    FAILED = "failed"


@dataclass(slots=True)
class PluginInfo:
    """
    One discovered plugin.

    Attributes:
        plugin: The plugin instance.
        source: Where it was found.
        state: Whether it is active, and if not, why.
        detail: Why a plugin is not active, for the admin UI.
    """

    plugin: UrbanLensPlugin
    source: PluginSource
    state: PluginState = PluginState.ACTIVE
    detail: str = ""

    @property
    def name(self) -> str:
        """The plugin's name."""
        return self.plugin.name

    @property
    def is_active(self) -> bool:
        """Whether the plugin's contributions are in effect."""
        return self.state is PluginState.ACTIVE

    @property
    def app_label(self) -> str | None:
        """The Django app label of a package plugin, which is its name; None for a module plugin."""
        return self.plugin.name if self.source.is_package else None


class PluginRegistry:
    """Holds every discovered plugin. Ordered by ``(order, name)`` wherever plugins are listed."""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginInfo] = {}
        self._discovered = False

    def discover(self, *, force: bool = False) -> None:
        """
        Import, resolve and set up every plugin; idempotent unless ``force``.

        Runs inside ``AppConfig.ready()``, so nothing here may touch the database.

        Args:
            force: Withdraw every plugin contribution and run discovery again (for tests).
        """
        if self._discovered and not force:
            return
        from urbanlens.UrbanLens.settings.app import settings

        self._reset()
        self._discovered = True
        disabled = frozenset(settings.disabled_plugins)
        for source in find_plugin_sources(settings.plugin_modules):
            self._load(source, disabled=disabled)
        self._resolve_requirements()

        for info in self.plugins():
            if info.is_active:
                self._setup(info)

        from urbanlens.dashboard.plugins.points import PLUGINS_LOADED

        PLUGINS_LOADED.fire(self)
        active = [info.name for info in self.plugins() if info.is_active]
        logger.info("Plugins: %d found, %d active (%s)", len(self._plugins), len(active), ", ".join(active))

    def register(self, plugin: UrbanLensPlugin | type[UrbanLensPlugin], *, source: PluginSource | None = None) -> PluginInfo | None:
        """
        Register and set up one plugin from code - chiefly for tests. Discovery registers everything else.

        Returns:
            Its record, or None when it was rejected (no name, a duplicate name, or it could not be instantiated).
        """
        instance = self._instantiate(plugin)
        if instance is None:
            return None
        info = self._add(instance, source or PluginSource(name=instance.name, module=type(instance).__module__, origin="settings", is_package=False))
        if info is not None and info.is_active:
            self._setup(info)
        return info

    def unregister(self, name: str) -> bool:
        """
        Remove one plugin and withdraw its contributions - chiefly for tests.

        Returns:
            True when it was registered.
        """
        info = self._plugins.pop(name, None)
        if info is None:
            return False
        discard_contributions(owner=name)
        return True

    def plugins(self) -> list[PluginInfo]:
        """Every discovered plugin, ordered by ``(order, name)``, whatever its state."""
        return sorted(self._plugins.values(), key=lambda info: (info.plugin.order, info.plugin.name))

    def info(self, name: str) -> PluginInfo | None:
        """The record of one plugin, or None when it was not found."""
        return self._plugins.get(name)

    def get(self, name: str) -> UrbanLensPlugin | None:
        """One discovered plugin, active or not, or None."""
        info = self._plugins.get(name)
        return info.plugin if info else None

    def is_active(self, name: str) -> bool:
        """Whether a plugin was found and its contributions are in effect."""
        info = self._plugins.get(name)
        return info is not None and info.is_active

    def active_plugins(self) -> list[UrbanLensPlugin]:
        """Active plugins, ordered by ``(order, name)``."""
        return [info.plugin for info in self.plugins() if info.is_active]

    # Internals

    def _load(self, source: PluginSource, *, disabled: frozenset[str]) -> None:
        try:
            module = importlib.import_module(source.plugin_module)
        except Exception:
            logger.exception("Could not import plugin %r from %s", source.name, source.plugin_module)
            return
        if source.attribute:
            candidates = [getattr(module, source.attribute, None)]
        else:
            candidates = [value for value in vars(module).values() if inspect.isclass(value) and issubclass(value, UrbanLensPlugin) and value.__module__ == module.__name__ and value.name]
        for candidate in candidates:
            instance = self._instantiate(candidate)
            if instance is None:
                continue
            if source.is_package and instance.name != source.name:
                logger.error("Ignoring plugin %r in %s: a package plugin's name must be its package name (%r)", instance.name, source.module, source.name)
                continue
            info = self._add(instance, source)
            if info is not None and instance.name in disabled:
                info.state = PluginState.DISABLED
                info.detail = "Listed in UL_DISABLED_PLUGINS"

    @staticmethod
    def _instantiate(plugin: object) -> UrbanLensPlugin | None:
        try:
            if isinstance(plugin, type) and issubclass(plugin, UrbanLensPlugin):
                return plugin()
            if isinstance(plugin, UrbanLensPlugin):
                return plugin
        except Exception:
            logger.exception("Could not instantiate plugin %r", plugin)
            return None
        logger.error("%r is not an UrbanLensPlugin class or instance", plugin)
        return None

    def _add(self, instance: UrbanLensPlugin, source: PluginSource) -> PluginInfo | None:
        if not instance.name:
            logger.warning("Ignoring plugin %s: it sets no name", type(instance).__qualname__)
            return None
        if instance.name in self._plugins:
            logger.warning("Ignoring plugin %r from %s: the name is already taken by %s", instance.name, source.module, self._plugins[instance.name].source.module)
            return None
        info = PluginInfo(plugin=instance, source=source)
        self._plugins[instance.name] = info
        return info

    def _resolve_requirements(self) -> None:
        # A plugin left inactive can leave another inactive in turn, so repeat until nothing changes.
        changed = True
        while changed:
            changed = False
            for info in self._plugins.values():
                if not info.is_active:
                    continue
                unmet = [name for name in info.plugin.requires if not self.is_active(name)]
                if unmet:
                    info.state = PluginState.MISSING_REQUIREMENT
                    info.detail = "Requires " + ", ".join(unmet)
                    changed = True

    def _setup(self, info: PluginInfo) -> None:
        try:
            info.plugin.setup(PluginContext(info.plugin))
        except Exception:
            logger.exception("Plugin %r setup() failed; withdrawing its contributions", info.name)
            discard_contributions(owner=info.name)
            info.state = PluginState.FAILED
            info.detail = "setup() raised; see the log"

    def _reset(self) -> None:
        for name in self._plugins:
            discard_contributions(owner=name)
        self._plugins.clear()
        self._discovered = False


#: The application-wide registry, populated in the dashboard app's ``ready()``.
plugin_registry = PluginRegistry()
