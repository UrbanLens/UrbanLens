"""
Find plugins before Django starts, so package plugins can be installed as Django apps.

Runs while settings load: it may not import Django models, the app registry, or a plugin's own code. It only locates
modules - a package plugin's models, migrations, templates and static files arrive by its being in
``INSTALLED_APPS`` - and :class:`~urbanlens.dashboard.plugins.registry.PluginRegistry` imports them once apps are
ready.

Sources, in the order a duplicate name is resolved (first wins):

1. Bundled package plugins: each subpackage of :mod:`urbanlens.plugins`. A deployment adds or removes a feature by
   adding or removing its directory.
2. Bundled module plugins: each module of :mod:`urbanlens.dashboard.plugins.builtin`.
3. ``UL_PLUGIN_MODULES``: dotted paths of further modules or packages.
4. The ``urbanlens.plugins`` entry-point group of installed distributions; the entry-point name is the plugin name.

A plugin named in ``UL_DISABLED_PLUGINS`` is still found (the admin UI lists it) but its package is not installed as
an app and it contributes nothing. Its tables, if it has any, are left as they are.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
import importlib.util
import logging
from pathlib import Path
import pkgutil
from typing import Literal

logger = logging.getLogger(__name__)

#: Entry-point group installed distributions expose plugins under.
ENTRY_POINT_GROUP = "urbanlens.plugins"

#: The package whose subpackages are bundled package plugins.
BUNDLED_PACKAGE = "urbanlens.plugins"

#: The package whose modules are bundled module plugins.
BUILTIN_PACKAGE = "urbanlens.dashboard.plugins.builtin"

#: The directory in a package plugin whose templates replace core's of the same name.
TEMPLATE_OVERRIDES_DIR = "template_overrides"

Origin = Literal["bundled", "builtin", "settings", "entry-point"]


@dataclass(frozen=True, slots=True)
class PluginSource:
    """
    Where one plugin lives, found without importing it.

    Attributes:
        name: The plugin's expected name: a package or module's last segment, or an entry point's name.
        module: Dotted path of the plugin's module or package.
        origin: Which discovery source found it.
        is_package: Whether it is a package, and so installed as a Django app.
        attribute: For an entry point naming one object (``module:attr``), that attribute.
    """

    name: str
    module: str
    origin: Origin
    is_package: bool
    attribute: str | None = None

    @property
    def plugin_module(self) -> str:
        """The module the plugin class is defined in: a package's ``plugin`` submodule, else the module itself."""
        return f"{self.module}.plugin" if self.is_package else self.module


def _is_package(module: str) -> bool | None:
    """Whether ``module`` is a package, a plain module (False), or cannot be found (None)."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    return spec.submodule_search_locations is not None


def _iter_package(package: str, *, packages: bool) -> list[str]:
    """Names of the subpackages (or plain modules) directly inside ``package``; empty when it does not exist."""
    try:
        spec = importlib.util.find_spec(package)
    except (ImportError, ValueError):
        return []
    if spec is None or spec.submodule_search_locations is None:
        return []
    return sorted(info.name for info in pkgutil.iter_modules(spec.submodule_search_locations) if info.ispkg == packages and not info.name.startswith("_"))


def find_plugin_sources(extra_modules: list[str] | tuple[str, ...] = ()) -> list[PluginSource]:
    """
    Every plugin this install can see, in precedence order, without importing any plugin.

    Args:
        extra_modules: ``UL_PLUGIN_MODULES``.

    Returns:
        One source per plugin name; a later source with a name already found is logged and dropped.
    """
    found: list[PluginSource] = [PluginSource(name=name, module=f"{BUNDLED_PACKAGE}.{name}", origin="bundled", is_package=True) for name in _iter_package(BUNDLED_PACKAGE, packages=True)]
    found += [PluginSource(name=name, module=f"{BUILTIN_PACKAGE}.{name}", origin="builtin", is_package=False) for name in _iter_package(BUILTIN_PACKAGE, packages=False)]

    for module in extra_modules:
        is_package = _is_package(module)
        if is_package is None:
            logger.error("UL_PLUGIN_MODULES names %r, which cannot be found", module)
            continue
        found.append(PluginSource(name=module.rpartition(".")[2], module=module, origin="settings", is_package=is_package))

    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        logger.exception("Could not list the %r entry points", ENTRY_POINT_GROUP)
        eps = []
    for ep in eps:
        module, _, attribute = ep.value.partition(":")
        module = module.strip()
        is_package = _is_package(module)
        if is_package is None:
            logger.error("Plugin entry point %r names %r, which cannot be found", ep.name, module)
            continue
        found.append(PluginSource(name=ep.name, module=module, origin="entry-point", is_package=is_package and not attribute, attribute=attribute.strip() or None))

    unique: dict[str, PluginSource] = {}
    for source in found:
        if source.name in unique:
            logger.warning("Ignoring plugin %r from %s: the name is already taken by %s", source.name, source.module, unique[source.name].module)
            continue
        unique[source.name] = source
    return list(unique.values())


def plugin_apps(sources: list[PluginSource], disabled: list[str] | tuple[str, ...] | frozenset[str]) -> list[str]:
    """
    The ``INSTALLED_APPS`` entries for the enabled package plugins.

    Args:
        sources: From :func:`find_plugin_sources`.
        disabled: ``UL_DISABLED_PLUGINS``.

    Returns:
        Each enabled package plugin's module path, in source order.
    """
    return [source.module for source in sources if source.is_package and source.name not in disabled]


def template_override_dirs(sources: list[PluginSource], disabled: list[str] | tuple[str, ...] | frozenset[str]) -> list[str]:
    """
    The template directories through which enabled package plugins replace core templates.

    A package plugin's own ``templates/`` is searched after core's, so it can only add templates. One that ships a
    :data:`TEMPLATE_OVERRIDES_DIR` replaces any core template it holds a file of the same name for, and that file can
    ``{% extends %}`` the core template it replaces to override single blocks.

    Args:
        sources: From :func:`find_plugin_sources`.
        disabled: ``UL_DISABLED_PLUGINS``.

    Returns:
        Each such directory, in source order.
    """
    found: list[str] = []
    for source in sources:
        if not source.is_package or source.name in disabled:
            continue
        try:
            spec = importlib.util.find_spec(source.module)
        except (ImportError, ValueError):
            continue
        for location in (spec.submodule_search_locations or []) if spec else []:
            if (directory := Path(location) / TEMPLATE_OVERRIDES_DIR).is_dir():
                found.append(str(directory))
    return found
