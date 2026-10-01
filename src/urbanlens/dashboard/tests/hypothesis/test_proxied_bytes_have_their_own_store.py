"""Bytes proxied from somewhere else do not share a store with the sessions.

A tile is cached per layer and coordinate, so the number of keys is the number of coordinates
anyone looked at in a week. The store holding them is 1 GiB and refuses writes rather than
evicting once full, and it also holds every session and the Channels layer - so at the scale this
deployment is being sized for, panning around a map decides whether anyone can log in.

Most of these cover the wiring, because that is where the rest of the property lives: what
separates the two stores in a deployment is which Dragonfly they connect to and whether it may
evict, neither of which locmem has. The aliases do get their own locmem instance under
`settings/test.py`, though, so anything that writes proxied bytes to the wrong one is visible here.
"""

from __future__ import annotations

import ast
import pathlib
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.core.management import call_command
from model_bakery import baker
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.locations.redata_basemap_tiles_gateway import RedataBasemapTilesGateway
from urbanlens.dashboard.services.core import bounded_cache
from urbanlens.dashboard.services.map.tile_cache_keys import basemap_tile_cache_key

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
COMPOSE = REPO_ROOT / "docker-compose.yml"

_CONFIGURED = "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured"


def _tile_cache_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["tile-cache"]


def _flags(command: list[str]) -> dict[str, str]:
    return dict(part.lstrip("-").partition("=")[::2] for part in command if part.startswith("--"))


class ProxiedBytesGoToTheirOwnAliasTests(SimpleTestCase):
    def test_the_alias_exists_and_is_not_the_default_one(self) -> None:
        self.assertNotEqual(settings.PROXIED_BYTES_CACHE, "default")
        self.assertIn(settings.PROXIED_BYTES_CACHE, settings.CACHES)
        # Resolvable, not merely named: a typo'd alias raises only on the first proxied body.
        self.assertIsNotNone(caches[settings.PROXIED_BYTES_CACHE])

    def test_every_helper_reads_and_writes_that_alias(self) -> None:
        """One helper still on ``default`` would put a tile back beside the sessions, and no test
        under these settings could see it."""
        source = pathlib.Path(bounded_cache.__file__).read_text()
        tree = ast.parse(source)
        functions = [
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")
        ]
        self.assertTrue(functions)

        for function in functions:
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            reached = {node.func.id for node in calls if isinstance(node.func, ast.Name)}
            touches_cache = any(
                isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "get_many", "set", "delete"}
                for node in calls
            )
            with self.subTest(helper=function.name):
                self.assertTrue(
                    "_store" in reached or not touches_cache,
                    f"{function.name} reaches a cache without going through _store()",
                )

        self.assertNotIn("from django.core.cache import cache\n", source, "the module-level default cache is back")

    def test_nothing_spells_a_tile_key_by_hand(self) -> None:
        """Anti-drift for the seeder tests below: one writer building the f-string itself is how
        the seeder and the proxy came apart, and a second store makes that silent rather than loud."""
        owner = pathlib.Path(REPO_ROOT / "src/urbanlens/dashboard/services/map/tile_cache_keys.py")
        sources = [
            path
            for path in (REPO_ROOT / "src/urbanlens").rglob("*.py")
            if path != owner and "/tests/" not in str(path) and "/migrations/" not in str(path)
        ]
        self.assertTrue(sources)

        for path in sources:
            text = path.read_text()
            for prefix in ('f"ul_basemap_tile_', 'f"ul_histmap_tile_'):
                with self.subTest(module=str(path.relative_to(REPO_ROOT)), prefix=prefix):
                    self.assertNotIn(prefix, text, "builds a tile cache key instead of importing one")

    def test_the_tile_authorisation_grant_stays_in_the_same_store_as_the_tile(self) -> None:
        """The proxy reads the grant and the tile in one round trip, which stops being one the
        moment they live in different instances."""
        source = pathlib.Path(REPO_ROOT / "src/urbanlens/dashboard/services/map/tile_authorisation.py").read_text()

        self.assertIn("bounded_cache.set_or_skip", source)
        self.assertIn("bounded_cache.delete_quietly", source)
        self.assertNotIn("from django.core.cache import cache", source)


class TheProxiedBytesStoreIsProvisionedTests(SimpleTestCase):
    """An alias pointed at the instance it was split away from is the arrangement it exists to end."""

    def test_compose_runs_a_second_instance_for_it(self) -> None:
        self.assertEqual(
            _tile_cache_service()["image"], yaml.safe_load(COMPOSE.read_text())["services"]["dragonfly"]["image"]
        )

    def test_it_may_evict_where_the_shared_store_may_not(self) -> None:
        self.assertEqual(_flags(_tile_cache_service()["command"]).get("cache_mode"), "true")
        self.assertNotIn("cache_mode", _flags(yaml.safe_load(COMPOSE.read_text())["services"]["dragonfly"]["command"]))

    def test_dragonfly_refuses_to_start_below_256mib_per_thread(self) -> None:
        """Its own startup check, so a thread count raised without the memory to back it takes the
        stack down rather than degrading."""
        flags = _flags(_tile_cache_service()["command"])
        threads = int(flags["proactor_threads"].partition(":-")[2].rstrip("}") or flags["proactor_threads"])
        megabytes = flags["maxmemory"].partition(":-")[2].rstrip("}")

        self.assertGreaterEqual(int(megabytes.removesuffix("mb")), 256 * threads)

    def test_every_service_that_proxies_bytes_is_told_where_to_put_them(self) -> None:
        """A service left without it falls back to the shared store, silently."""
        compose = yaml.safe_load(COMPOSE.read_text())
        for name in ("app", "celery-worker", "app-ws"):
            with self.subTest(service=name):
                self.assertIn("UL_PROXY_CACHE_URL", compose["services"][name]["environment"])

    def test_the_app_waits_for_it(self) -> None:
        compose = yaml.safe_load(COMPOSE.read_text())
        self.assertEqual(compose["services"]["app"]["depends_on"]["tile-cache"]["condition"], "service_healthy")

    def test_it_is_reachable_from_every_service_that_is_told_about_it(self) -> None:
        """The alias only resolves on the networks the service joins; elsewhere the cache silently
        degrades to unreachable, which looks exactly like a cache that is simply cold."""
        compose = yaml.safe_load(COMPOSE.read_text())
        joined = set(_tile_cache_service()["networks"])
        told = {
            name: set(service.get("networks") or ())
            for name, service in compose["services"].items()
            if "UL_PROXY_CACHE_URL" in (service.get("environment") or {})
        }

        self.assertTrue(told, "nothing is configured to use it")
        for name, networks in told.items():
            with self.subTest(service=name):
                self.assertTrue(networks & joined, f"{name} is pointed at it on none of {sorted(networks)}")


class WhatSeedsATileWritesWhereTheProxyReadsTests(TestCase):
    """The load run's seeder addresses the same bytes the proxy does, from a different process.

    It wrote them to the default cache while the proxy had moved to its own store, and nothing
    failed: a miss simply goes upstream, so the map still drew and the capacity run silently
    measured the fetch path it exists to avoid. The pre-flight check in `tests/perf/k6` caught it;
    these make it a test failure instead.
    """

    def _seed(self, size: int = 1) -> None:
        call_command(
            "seed_basemap_tile_cache",
            "--layer",
            "terrain",
            "--zoom",
            "13",
            "--origin-x",
            "2400",
            "--origin-y",
            "3072",
            "--size",
            str(size),
        )

    def test_the_seeder_writes_where_the_proxy_looks(self) -> None:
        self._seed(size=2)

        for x, y in ((2400, 3072), (2401, 3073)):
            key = basemap_tile_cache_key("terrain", 13, x, y)
            with self.subTest(tile=(x, y)):
                self.assertIsNotNone(
                    bounded_cache.get_or_none(key, label="seeded tile"), f"{key} is not in the store the proxy reads"
                )

    def test_a_seeded_tile_is_served_without_reaching_upstream(self) -> None:
        """The property the run actually depends on, end to end."""
        self.client.force_login(baker.make(get_user_model()))
        self._seed()

        with (
            mock.patch(_CONFIGURED, return_value=True),
            mock.patch.object(
                RedataBasemapTilesGateway,
                "download_tile",
                side_effect=AssertionError("went upstream for a seeded tile"),
            ),
        ):
            response = self.client.get("/dashboard/map/basemap-tiles/terrain/13/2400/3072/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")


class TheDefaultsStayInStepTests(SimpleTestCase):
    def test_the_compose_default_and_the_settings_fallback_name_the_same_thing(self) -> None:
        """Compose names the container; settings fall back to the shared store when nothing does.
        A rename on one side alone puts every proxied body back where it started."""
        compose = yaml.safe_load(COMPOSE.read_text())
        url = compose["services"]["app"]["environment"]["UL_PROXY_CACHE_URL"]
        alias = next(iter(_tile_cache_service()["networks"].values()))["aliases"][0]

        self.assertIn(alias, url.partition(":-")[2])

    def test_the_helper_and_the_alias_are_spelled_once(self) -> None:
        """Anti-vacuity for the tests above: they read the name off settings, so a settings module
        that named the default alias would make all of them pass."""
        base = pathlib.Path(REPO_ROOT / "src/urbanlens/UrbanLens/settings/base.py").read_text()
        assignments = [line for line in base.splitlines() if line.startswith("PROXIED_BYTES_CACHE")]

        self.assertEqual(len(assignments), 1)
        self.assertNotIn('"default"', assignments[0])
