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

#: Gateways left unregistered on purpose, each pending its own decision.
KNOWN_UNREGISTERED: frozenset[str] = frozenset()

#: Providers whose requests are made, and limited, by the REData gateway they build; nothing is spent under
#: their own key, which only names their cache and log entries.
DELEGATING_PROVIDERS = frozenset(
    {
        "chronicling_america",
        "internet_archive",
        "kartaview",
        "library_of_congress",
        "mapillary",
        "panoramax",
        "smithsonian",
    },
)

#: Keys held only by bases that are instantiated through subclasses carrying keys of their own.
SUBCLASSED_BASES = frozenset(
    {
        "media_provider",
        "redata_json",
        "redata_location_context",
        "street_view_provider",
        "twilio",
    },
)

_EXEMPT = KNOWN_UNREGISTERED | DELEGATING_PROVIDERS | SUBCLASSED_BASES


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
                if gw.service_key not in registered and gw.service_key not in _EXEMPT
            },
        )

        self.assertEqual(unregistered, [], "these gateways fall back to get_limit_config's generic limits")

    def test_the_exemptions_are_still_unregistered_gateway_keys(self) -> None:
        """A key registered since, or no longer any gateway's, must leave its set."""
        registered = all_service_defaults()
        live_keys = {gw.service_key for gw in _concrete_gateways()}
        stale = sorted(key for key in _EXEMPT if key in registered or key not in live_keys)

        self.assertEqual(stale, [])

    def test_a_delegating_provider_never_uses_a_session_of_its_own(self) -> None:
        own_session_users = sorted(
            f"{gw.__qualname__} via {base.__qualname__}"
            for gw in _concrete_gateways()
            if gw.service_key in DELEGATING_PROVIDERS
            for base in gw.__mro__
            if base.__module__.startswith(apis.__name__) and "session" in inspect.getsource(base)
        )

        self.assertEqual(own_session_users, [])

    def test_a_subclassed_base_has_subclasses_with_keys_of_their_own(self) -> None:
        gateways = _concrete_gateways()
        bare = sorted(
            key
            for key in SUBCLASSED_BASES
            if not any(
                sub.service_key and sub.service_key != key
                for gw in gateways
                if gw.service_key == key
                for sub in _all_subclasses(gw)
            )
        )

        self.assertEqual(bare, [])


class HistoricalMapTileLimitTests(SimpleTestCase):
    def test_historical_map_tiles_have_no_daily_cap(self) -> None:
        """The overlay tile proxy spends one call per uncached tile, so a daily cap blanks overlays for the rest of the day."""
        defaults = all_service_defaults().get("redata_historical_maps")

        self.assertIsNotNone(defaults)
        assert defaults is not None
        self.assertIsNone(defaults.calls_per_day)
