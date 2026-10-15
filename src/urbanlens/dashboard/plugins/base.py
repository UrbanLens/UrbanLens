"""Base class for UrbanLens plugins, and the context a plugin contributes through."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Concatenate

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.core.checks import CheckMessage

    from urbanlens.dashboard.plugins.extensions import ActionHook, ExtensionPoint, FilterHook, Registry, TemplateSlot


class UrbanLensPlugin:
    """
    One pluggable feature or integration: metadata plus a :meth:`setup` that contributes to core seams.

    A plugin is either a module (a light integration that only contributes providers) or a package, which is also
    installed as a Django app and so may carry models, migrations, templates, static files, ``admin.py``,
    ``tasks.py`` and management commands. See ``docs/plugins/README.md``.

    Attributes:
        name: Unique slug. For a package plugin it is also the Django app label, and must match the package name.
        verbose_name: Human-readable name for the admin UI.
        description: One or two sentences for the admin UI.
        version: Plugin version string.
        author: Shown in the admin UI.
        order: Default priority of this plugin's contributions; lower comes first.
        requires: Names of plugins this one needs. When any is missing or disabled this plugin stays inactive.
    """

    name: ClassVar[str] = ""
    verbose_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0"
    author: ClassVar[str] = ""
    order: ClassVar[int] = 100
    requires: ClassVar[tuple[str, ...]] = ()

    def setup(self, plugin: PluginContext) -> None:
        """
        Contribute to core seams. Called once, after every app is ready, only for an enabled plugin.

        Must not touch the database: it runs during startup, including before migrations exist.
        """

    def check(self) -> list[CheckMessage]:
        """
        Django system-check messages about this plugin's configuration, such as a missing API key.

        Returns:
            Messages; empty when the plugin is correctly configured.
        """
        return []


class PluginContext:
    """
    What :meth:`UrbanLensPlugin.setup` contributes through.

    Every contribution is recorded as the plugin's, so disabling or reloading the plugin withdraws exactly what it
    added. A contribution that names no priority takes the plugin's ``order``.
    """

    def __init__(self, plugin: UrbanLensPlugin) -> None:
        self.plugin = plugin

    def _priority(self, priority: int | None) -> int:
        return self.plugin.order if priority is None else priority

    def add[T](self, point: ExtensionPoint[T], value: T, *, priority: int | None = None) -> T:
        """Contribute one value to an extension point."""
        return point.add(value, owner=self.plugin.name, priority=self._priority(priority))

    def extend[T](self, point: ExtensionPoint[T], values: list[T], *, priority: int | None = None) -> None:
        """Contribute several values to an extension point, in order."""
        for value in values:
            self.add(point, value, priority=priority)

    def register[K, T](self, registry: Registry[K, T], key: K, value: T, *, priority: int | None = None) -> T:
        """Contribute one keyed value to a registry."""
        return registry.add(key, value, owner=self.plugin.name, priority=self._priority(priority))

    def register_all[K, T](self, registry: Registry[K, T], values: dict[K, T], *, priority: int | None = None) -> None:
        """Contribute several keyed values to a registry."""
        for key, value in values.items():
            self.register(registry, key, value, priority=priority)

    def filter[T, **P](self, hook: FilterHook[T, P], callback: Callable[Concatenate[T, P], T], *, priority: int | None = None) -> None:
        """Connect a filter callback."""
        hook.connect(callback, owner=self.plugin.name, priority=self._priority(priority))

    def on[**P](self, hook: ActionHook[P], callback: Callable[P, object], *, priority: int | None = None) -> None:
        """Connect an action callback."""
        hook.connect(callback, owner=self.plugin.name, priority=self._priority(priority))

    def template(self, slot: TemplateSlot, template_name: str, *, priority: int | None = None) -> None:
        """Render a template in a core template slot."""
        self.add(slot, template_name, priority=priority)
