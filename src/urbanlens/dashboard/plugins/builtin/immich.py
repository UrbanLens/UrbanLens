"""
Immich plugin: rate-limit defaults for the per-user photo import integration.
This plugin registers the service's rate-limit defaults so calls are throttled and logged like every other external API."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.core.rate_limiter import ServiceDefaults
from urbanlens.UrbanLens.egress import EgressCategory

if TYPE_CHECKING:
    from urbanlens.dashboard.plugins.base import PluginContext


class ImmichPlugin(UrbanLensPlugin):
    """Immich integration: per-user photo search and import."""

    name: ClassVar[str] = "immich"
    verbose_name: ClassVar[str] = "Immich"
    description: ClassVar[str] = "Searches a user's self-hosted Immich server for photos near a pin and imports selected ones."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 40

    def setup(self, plugin: PluginContext) -> None:
        """Rate-limit defaults for Immich, which is self-hosted per user, so the limits only guard against runaway loops."""
        from urbanlens.dashboard.plugins.points import SERVICE_DEFAULTS

        plugin.register_all(
            SERVICE_DEFAULTS,
            {
                "immich": ServiceDefaults(
                    display_name="Immich",
                    category=EgressCategory.INTERNAL,
                    calls_per_minute=60,
                    calls_per_day=5000,
                    notes="Self-hosted per user - each user's calls hit their own server, not a shared quota, so limits here mainly guard against runaway loops.",
                ),
            },
        )
