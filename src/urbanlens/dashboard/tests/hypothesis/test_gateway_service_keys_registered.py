"""Every gateway's service key must name a registered rate-limit default.

An unregistered key is not refused: `get_limit_config` creates its row from a generic
fallback nobody chose for that integration, and `test_plugin_rate_limit_coverage` cannot
see it, because it only inspects keys that are registered (P93).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.plugins.registry import plugin_registry
from urbanlens.dashboard.services import apis
from urbanlens.dashboard.services.core.gateway import Gateway
from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

#: Unregistered when this test was written; each needs its own decision (P93). A key leaves
#: this set when it is registered or its gateway stops making calls of its own.
KNOWN_UNREGISTERED = frozenset(
    {
        "chronicling_america",
        "google_open_buildings",
        "internet_archive",
        "kartaview",
        "library_of_congress",
        "mapillary",
        "media_provider",
        "microsoft_building_footprints",
        "overture_maps",
        "panoramax",
        "redata_historical_maps",
        "redata_json",
        "redata_location_context",
        "redata_media",
        "redata_nature_observations",
        "redata_search_news",
        "redata_search_web",
        "redata_street_view",
        "smithsonian",
        "street_view_provider",
        "twilio",
    },
)


def _all_subclasses[T](cls: type[T]) -> set[type[T]]:
    found: set[type[T]] = set()
    pending = [cls]
    while pending:
        for sub in pending.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                pending.append(sub)
    return found


def _concrete_gateways() -> list[type[Gateway]]:
    for module in pkgutil.walk_packages(apis.__path__, prefix=f"{apis.__name__}."):
        importlib.import_module(module.name)
    return sorted(
        (gw for gw in _all_subclasses(Gateway) if not inspect.isabstract(gw) and gw.service_key),
        key=lambda gw: gw.__qualname__,
    )


class GatewayServiceKeyRegisteredTests(SimpleTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        plugin_registry.discover()

    def test_the_walk_found_gateways(self) -> None:
        """Guards the check below from passing on an empty walk."""
        self.assertGreater(len(_concrete_gateways()), 30)

    def test_every_gateway_service_key_is_registered(self) -> None:
        registered = all_service_defaults()
        unregistered = sorted(
            {
                f"{gw.service_key} ({gw.__module__}.{gw.__qualname__})"
                for gw in _concrete_gateways()
                if gw.service_key not in registered and gw.service_key not in KNOWN_UNREGISTERED
            },
        )

        self.assertEqual(unregistered, [], "these gateways fall back to get_limit_config's generic limits")

    def test_the_known_gaps_are_still_gaps(self) -> None:
        """A key registered since, or no longer any gateway's, must leave the list."""
        registered = all_service_defaults()
        live_keys = {gw.service_key for gw in _concrete_gateways()}
        stale = sorted(key for key in KNOWN_UNREGISTERED if key in registered or key not in live_keys)

        self.assertEqual(stale, [])
