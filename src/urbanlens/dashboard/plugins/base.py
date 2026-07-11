"""Base class for UrbanLens plugins.

A plugin bundles one external integration (usually one third-party API) into
a single self-contained unit: its metadata, its default rate-limit
configuration, and the concrete contributions it makes to the application
(pin-detail panels, imagery providers, hook callbacks, ...).

Plugins are discovered by the
:class:`~urbanlens.dashboard.plugins.registry.PluginRegistry` from three
places:

1. Bundled plugins in :mod:`urbanlens.dashboard.plugins.builtin`.
2. Third-party pip packages exposing the ``urbanlens.plugins`` entry-point
   group.
3. Extra dotted module paths listed in the ``UL_PLUGIN_MODULES`` setting.

Plugin classes are instantiated once during discovery, which runs inside
``AppConfig.ready()``. **Neither import nor ``__init__`` may touch the
database or the network** - defer all real work to the contribution objects,
which run lazily at request/Celery time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.hooks import HookRegistry
    from urbanlens.dashboard.services.apis.locations.base import SatelliteViewProvider, StreetViewProvider
    from urbanlens.dashboard.services.external_data import PanelSource
    from urbanlens.dashboard.services.locations.name_resolution import NameProvider
    from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class UrbanLensPlugin:
    """One pluggable integration: metadata plus typed contribution points.

    Subclasses set ``name`` (a unique slug) and override whichever
    contribution methods apply - every contribution is optional, so this is
    deliberately a plain class rather than an ABC. A class without a ``name``
    is treated as an abstract intermediate base and skipped by discovery.

    Attributes:
        name: Unique plugin slug (e.g. ``"nps"``). Required for discovery.
        verbose_name: Human-readable name shown in the admin UI.
        description: One-or-two sentence summary for the admin UI.
        version: Plugin version string.
        author: Plugin author, shown in the admin UI.
        order: Sort key for aggregated contributions (e.g. the order imagery
            providers appear in a carousel). Lower sorts earlier; defaults
            to 100.
    """

    name: ClassVar[str] = ""
    verbose_name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "1.0"
    author: ClassVar[str] = ""
    order: ClassVar[int] = 100

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Default rate-limit configuration for this plugin's service keys.

        Feeds the same machinery as ``rate_limiter.SERVICE_REGISTRY``: an
        ``ApiRateLimit`` row is auto-created from these defaults the first
        time each service key is used, after which admins manage the row via
        the site-admin API limits page.

        Returns:
            Mapping of service key to its defaults; empty when the plugin
            makes no rate-limited API calls.
        """
        return {}

    def get_panel_sources(self) -> list[PanelSource]:
        """Pin-detail external-data panels contributed by this plugin.

        Returns:
            PanelSource instances to add to the panel registry; empty when
            the plugin contributes no panels.
        """
        return []

    def get_satellite_providers(self) -> list[SatelliteViewProvider]:
        """Satellite-imagery providers for the pin-detail satellite carousel.

        Called each time the carousel's provider chain runs, so returning
        freshly constructed gateway instances is expected. Providers from all
        plugins are concatenated in plugin ``order``.

        Returns:
            Provider gateway instances; empty when the plugin contributes
            no satellite imagery.
        """
        return []

    def get_name_providers(self) -> list[NameProvider]:
        """Place-name candidate providers contributed by this plugin.

        Providers yield raw name candidates for a location (usually read from
        the LocationCache rows this plugin's panels populate). Candidates from
        all plugins are cleaned, quality-gated, and resolved into the
        location's official name; each surviving candidate is also persisted
        as an official alias attributed to the provider's ``source`` slug.

        Returns:
            NameProvider instances; empty when the plugin contributes no
            place names.
        """
        return []

    def get_street_view_providers(self) -> list[StreetViewProvider]:
        """Street-level imagery providers for the pin-detail street carousel.

        Called each time the carousel's provider chain runs, so returning
        freshly constructed gateway instances is expected. Providers from all
        plugins are concatenated in plugin ``order``.

        Returns:
            Provider gateway instances; empty when the plugin contributes
            no street-level imagery.
        """
        return []

    def register(self, hooks: HookRegistry) -> None:
        """Attach action/filter callbacks to the shared hook bus.

        Called once per plugin after all plugins are discovered. Override to
        integrate with extension points that have no dedicated contribution
        method.

        Args:
            hooks: The shared :data:`~urbanlens.dashboard.plugins.hooks.hooks`
                registry.
        """
