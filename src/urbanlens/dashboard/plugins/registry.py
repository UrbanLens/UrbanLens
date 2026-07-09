"""Discovery and aggregation of UrbanLens plugins.

The module-level :data:`plugin_registry` singleton is populated once at app
startup (``DashboardConfig.ready()``) from three sources, in this order:

1. **Bundled** - every module in :mod:`urbanlens.dashboard.plugins.builtin`.
2. **Settings** - dotted module paths in the ``UL_PLUGIN_MODULES`` env
   setting, for site-local plugins that aren't packaged.
3. **Entry points** - pip-installed packages exposing the
   ``urbanlens.plugins`` entry-point group, for distributable plugins::

       [project.entry-points."urbanlens.plugins"]
       my_plugin = "my_package.urbanlens_plugin"

An entry point may resolve to a plugin class, a plugin instance, or a module
(which is scanned for plugin classes defined in it). A failure loading any
one plugin is logged and skipped so a broken plugin never prevents startup.

Install-level disabling: list plugin names in the ``UL_DISABLED_PLUGINS``
setting. Runtime per-service toggling (without a restart) remains on the
site-admin API limits page via ``ApiRateLimit.enabled``, which gates the
actual HTTP calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from importlib.metadata import entry_points
import inspect
import logging
import pkgutil
from types import ModuleType
from typing import TYPE_CHECKING

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.plugins.hooks import ACTION_PLUGINS_LOADED, hooks

if TYPE_CHECKING:
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider, StreetViewProvider
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.rate_limiter import ServiceDefaults

logger = logging.getLogger(__name__)

#: Entry-point group third-party packages use to expose plugins.
ENTRY_POINT_GROUP = "urbanlens.plugins"


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """One discovered plugin and where it came from.

    Attributes:
        plugin: The plugin instance.
        source: Discovery source: ``"builtin"``, ``"settings"``,
            ``"entry-point"``, or ``"code"`` (registered programmatically).
        module: Dotted path of the module the plugin class was defined in.
    """

    plugin: UrbanLensPlugin
    source: str
    module: str


class PluginRegistry:
    """Holds every discovered plugin and aggregates their contributions.

    Aggregation methods only consult *enabled* plugins, ordered by
    ``(plugin.order, plugin.name)`` so contribution ordering (e.g. imagery
    carousel provider order) is deterministic and plugin-controlled.
    """

    def __init__(self) -> None:
        """Initialize an empty, undiscovered registry."""
        self._plugins: dict[str, PluginInfo] = {}
        self._discovered = False

    def discover(self, *, force: bool = False) -> None:
        """Load plugins from all discovery sources; idempotent.

        Runs inside ``AppConfig.ready()``, so nothing here may touch the
        database. After all sources load, each plugin's ``register`` hook
        runs and the ``plugins_loaded`` action fires.

        Args:
            force: Re-run discovery from scratch, discarding current state.
        """
        if self._discovered and not force:
            return
        self._plugins.clear()
        self._discovered = True

        self._load_builtin()
        self._load_settings_modules()
        self._load_entry_points()

        for info in self.plugins():
            try:
                info.plugin.register(hooks)
            except Exception:
                logger.exception("Plugin '%s' register() failed", info.plugin.name)
        hooks.do_action(ACTION_PLUGINS_LOADED, self)
        logger.info("Discovered %d plugins: %s", len(self._plugins), ", ".join(sorted(self._plugins)))

    def register(self, plugin: UrbanLensPlugin | type[UrbanLensPlugin], *, source: str = "code") -> UrbanLensPlugin | None:
        """Register one plugin instance or class.

        Args:
            plugin: The plugin (a class is instantiated with no arguments).
            source: Discovery source label recorded for the admin UI.

        Returns:
            The registered instance, or None when the plugin was rejected
            (missing name, duplicate name, or failed instantiation).
        """
        try:
            instance = plugin() if isinstance(plugin, type) else plugin
        except Exception:
            logger.exception("Failed to instantiate plugin %r", plugin)
            return None

        if not instance.name:
            logger.warning("Ignoring plugin %r: it does not set a name", type(instance).__qualname__)
            return None
        if instance.name in self._plugins:
            logger.warning(
                "Ignoring duplicate plugin '%s' from %s: already registered from %s",
                instance.name,
                type(instance).__module__,
                self._plugins[instance.name].module,
            )
            return None

        self._plugins[instance.name] = PluginInfo(plugin=instance, source=source, module=type(instance).__module__)
        return instance

    def plugins(self) -> list[PluginInfo]:
        """Every discovered plugin, ordered by ``(order, name)``.

        Returns:
            All plugins regardless of enabled state (for the admin UI).
        """
        return sorted(self._plugins.values(), key=lambda info: (info.plugin.order, info.plugin.name))

    def get(self, name: str) -> UrbanLensPlugin | None:
        """Look up a plugin by name.

        Args:
            name: The plugin slug.

        Returns:
            The plugin instance, or None when not discovered.
        """
        info = self._plugins.get(name)
        return info.plugin if info else None

    def is_enabled(self, name: str) -> bool:
        """Whether a plugin is enabled for this install.

        Args:
            name: The plugin slug.

        Returns:
            True unless the name appears in the ``UL_DISABLED_PLUGINS``
            setting.
        """
        from urbanlens.UrbanLens.settings.app import settings

        return name not in settings.disabled_plugins

    def enabled_plugins(self) -> list[UrbanLensPlugin]:
        """Enabled plugins, ordered by ``(order, name)``.

        Returns:
            Plugin instances whose contributions should be active.
        """
        return [info.plugin for info in self.plugins() if self.is_enabled(info.plugin.name)]

    # ------------------------------------------------------------------
    # Contribution aggregation
    # ------------------------------------------------------------------

    def service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults declared by enabled plugins.

        Returns:
            Merged mapping of service key to defaults. A duplicate service
            key is logged and the first (lowest-ordered) plugin wins.
        """
        merged: dict[str, ServiceDefaults] = {}
        for plugin in self.enabled_plugins():
            for key, defaults in self._safe(plugin, "get_service_defaults", {}).items():
                if key in merged:
                    logger.warning("Plugin '%s' redeclares service defaults for '%s'; keeping the first declaration", plugin.name, key)
                    continue
                merged[key] = defaults
        return merged

    def panel_sources(self) -> list[PanelSource]:
        """Pin-detail panel sources contributed by enabled plugins.

        Returns:
            PanelSource instances in plugin order.
        """
        return self._collect("get_panel_sources")

    def name_providers(self) -> list[NameProvider]:
        """Place-name candidate providers contributed by enabled plugins.

        Returns:
            NameProvider instances in plugin order, which is the arrival
            order the name resolver uses to break ties among unprioritized
            sources.
        """
        return self._collect("get_name_providers")

    def satellite_providers(self) -> list[SatelliteViewProvider]:
        """The satellite-imagery provider chain, in plugin order.

        Returns:
            Freshly built provider gateway instances.
        """
        return self._collect("get_satellite_providers")

    def street_view_providers(self) -> list[StreetViewProvider]:
        """The street-level imagery provider chain, in plugin order.

        Returns:
            Freshly built provider gateway instances.
        """
        return self._collect("get_street_view_providers")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect(self, method_name: str) -> list:
        """Concatenate one list-valued contribution across enabled plugins."""
        collected: list = []
        for plugin in self.enabled_plugins():
            collected.extend(self._safe(plugin, method_name, []))
        return collected

    @staticmethod
    def _safe(plugin: UrbanLensPlugin, method_name: str, fallback):
        """Call one contribution method, logging (not raising) on failure."""
        try:
            return getattr(plugin, method_name)()
        except Exception:
            logger.exception("Plugin '%s' %s() failed", plugin.name, method_name)
            return fallback

    def _load_builtin(self) -> None:
        """Import and scan every module in the bundled plugins package."""
        from urbanlens.dashboard.plugins import builtin

        for module_info in pkgutil.iter_modules(builtin.__path__):
            module_path = f"{builtin.__name__}.{module_info.name}"
            try:
                module = importlib.import_module(module_path)
            except Exception:
                logger.exception("Failed to import builtin plugin module '%s'", module_path)
                continue
            self._register_module(module, source="builtin")

    def _load_settings_modules(self) -> None:
        """Import and scan the modules listed in the UL_PLUGIN_MODULES setting."""
        from urbanlens.UrbanLens.settings.app import settings

        for module_path in settings.plugin_modules:
            try:
                module = importlib.import_module(module_path)
            except Exception:
                logger.exception("Failed to import plugin module '%s' from UL_PLUGIN_MODULES", module_path)
                continue
            self._register_module(module, source="settings")

    def _load_entry_points(self) -> None:
        """Load plugins exposed via the ``urbanlens.plugins`` entry-point group."""
        try:
            eps = entry_points(group=ENTRY_POINT_GROUP)
        except Exception:
            logger.exception("Failed to enumerate '%s' entry points", ENTRY_POINT_GROUP)
            return

        for ep in eps:
            try:
                loaded = ep.load()
            except Exception:
                logger.exception("Failed to load plugin entry point '%s'", ep.name)
                continue
            if isinstance(loaded, ModuleType):
                self._register_module(loaded, source="entry-point")
            elif isinstance(loaded, UrbanLensPlugin) or (isinstance(loaded, type) and issubclass(loaded, UrbanLensPlugin)):
                self.register(loaded, source="entry-point")
            else:
                logger.warning("Plugin entry point '%s' resolved to %r, which is not a plugin class, instance, or module", ep.name, loaded)

    def _register_module(self, module: ModuleType, *, source: str) -> None:
        """Register every concrete plugin class defined in a module.

        Only classes *defined in* the module count - imported plugin classes
        are skipped so a module can reference another plugin without
        re-registering it.
        """
        for attr in vars(module).values():
            if inspect.isclass(attr) and issubclass(attr, UrbanLensPlugin) and attr.__module__ == module.__name__ and attr.name and not inspect.isabstract(attr):
                self.register(attr, source=source)

    def _reset(self) -> None:
        """Discard all state so tests can re-run discovery in isolation."""
        self._plugins.clear()
        self._discovered = False


#: The application-wide plugin registry, populated in ``AppConfig.ready()``.
plugin_registry = PluginRegistry()
