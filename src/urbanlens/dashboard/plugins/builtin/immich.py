"""Immich plugin: rate-limit defaults for the per-user photo import integration.

The integration itself (connect/disconnect, pin-detail search and import)
lives in ``dashboard/services/apis/immich/``, ``dashboard/controllers/immich.py``,
and ``dashboard/tasks.py``; every call runs against the requesting user's own
self-hosted server using their stored API key. This plugin registers the
service's rate-limit defaults so calls are throttled and logged like every
other external API.
"""

from __future__ import annotations

from typing import ClassVar

from urbanlens.dashboard.plugins.base import UrbanLensPlugin
from urbanlens.dashboard.services.rate_limiter import ServiceDefaults


class ImmichPlugin(UrbanLensPlugin):
    """Immich integration: per-user photo search and import."""

    name: ClassVar[str] = "immich"
    verbose_name: ClassVar[str] = "Immich"
    description: ClassVar[str] = "Searches a user's self-hosted Immich server for photos near a pin and imports selected ones."
    author: ClassVar[str] = "UrbanLens"
    order: ClassVar[int] = 40

    def get_service_defaults(self) -> dict[str, ServiceDefaults]:
        """Rate-limit defaults for the Immich API.

        Returns:
            Defaults for the ``immich`` service key. Generous compared to
            most integrations since every user's calls hit their own
            self-hosted server rather than a shared third-party quota.
        """
        return {
            "immich": ServiceDefaults(
                display_name="Immich",
                calls_per_minute=60,
                calls_per_day=5000,
                notes="Self-hosted per user - each user's calls hit their own server, not a shared quota, so limits here mainly guard against runaway loops.",
            ),
        }
