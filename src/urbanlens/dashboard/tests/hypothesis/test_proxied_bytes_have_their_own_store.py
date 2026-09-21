"""Bytes proxied from somewhere else do not share a store with the sessions.

A tile is cached per layer and coordinate, so the number of keys is the number of coordinates
anyone looked at in a week. The store holding them is 1 GiB and refuses writes rather than
evicting once full, and it also holds every session and the Channels layer - so at the scale this
deployment is being sized for, panning around a map decides whether anyone can log in.

These cover the wiring, because that is where the property lives: `settings/test.py` deliberately
points both aliases at one locmem instance (what separates them in a deployment is which Dragonfly
they connect to and whether it may evict, neither of which locmem has), so no behavioural test can
tell the two apart.
"""

from __future__ import annotations

import ast
import pathlib

from django.conf import settings
from django.core.cache import caches
import yaml

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core import bounded_cache

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]
COMPOSE = REPO_ROOT / "docker-compose.yml"


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
