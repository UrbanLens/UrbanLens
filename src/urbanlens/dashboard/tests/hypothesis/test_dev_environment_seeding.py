"""A new dev environment gets an account somebody can actually log into.

``bin/dev_env.py create`` used to hand back a URL onto an empty database: every
page an empty state, and no way in without building an account by hand. Worse,
running the demo seeder against it produced zero pins and logged "the location
pool is empty", which reads as a broken seeder rather than as an un-imported
catalog.

So the tests here pin the two halves that make the difference: the catalog is
populated before seeding when it can be, and when it cannot be, the account is
still created, still has a named landmark pin, and the reason travels back to
the caller as text instead of only into a log nobody reads.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
from unittest import mock

from django.contrib.auth.models import User
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.demo.seeding import (
    HUDSON_RIVER_STATE_HOSPITAL,
    seed_dev_environment,
    seed_landmark_pin,
)

#: Imported inside `redata_demo_locations`, so it is patched where it is defined.
_CATALOG = "urbanlens.dashboard.services.demo.locations.redata_demo_locations"

#: What `bin/opslib/devenv.py` derives from the environment slug.
_PASSWORD = "demo-a1b2c3"  # noqa: S105 - the point of this feature is that it is not secret

_ENTRIES = [
    {
        "latitude": 41.7658,
        "longitude": -72.6734,
        "official_name": "Connecticut State Capitol",
        "wiki": {"name": "Connecticut State Capitol", "aliases": [], "photos": []},
    },
    {
        "latitude": 42.3587,
        "longitude": -71.0637,
        "official_name": "Massachusetts State House",
        "wiki": {"name": "Massachusetts State House", "aliases": [], "photos": []},
    },
]


class SeedDevEnvironmentTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        # A manifest path of its own, so the run cannot read or write whichever
        # one the surrounding environment happens to have configured.
        self.manifest = Path(directory.name) / "manifest.json"
        patcher = mock.patch("urbanlens.UrbanLens.settings.app.settings.demo_locations_file", str(self.manifest))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _seed(self, entries: list[dict] | None = None) -> dict:
        with mock.patch(_CATALOG, return_value=entries if entries is not None else _ENTRIES):
            return seed_dev_environment(username="demo", password=_PASSWORD)

    def test_it_creates_an_account_with_the_password_it_reports(self) -> None:
        summary = self._seed()

        user = User.objects.get(username="demo")
        self.assertTrue(user.check_password(_PASSWORD))
        self.assertTrue(user.is_active)
        self.assertEqual(summary["username"], "demo")
        self.assertEqual(summary["password"], _PASSWORD)
        self.assertTrue(summary["created"])

    def test_the_account_is_outside_the_purge_prefix(self) -> None:
        """`purge_demo_accounts` selects on `demo-`; the account somebody was given must survive."""
        self._seed()

        self.assertFalse(User.objects.get(username="demo").username.startswith("demo-"))

    def test_the_catalog_is_imported_and_pinned(self) -> None:
        """The step the demo seeder does not do for itself, and the reason it produced no pins."""
        summary = self._seed()

        owner = User.objects.get(username="demo").profile
        pinned = set(Pin.objects.filter(profile=owner).values_list("location__official_name", flat=True))
        self.assertIn("Connecticut State Capitol", pinned)
        self.assertIn("Massachusetts State House", pinned)
        self.assertEqual(summary["pins"], Pin.objects.filter(profile=owner).count())
        self.assertIn("imported", summary["catalog"])

    def test_the_landmark_pin_is_created_and_named_on_the_pin(self) -> None:
        """`Location` has no name field of its own - the recognisable label belongs on the pin."""
        self._seed()

        owner = User.objects.get(username="demo").profile
        pin = Pin.objects.get(profile=owner, name=HUDSON_RIVER_STATE_HOSPITAL["name"])
        self.assertTrue(pin.name_is_user_provided)
        self.assertEqual(str(pin.location.latitude), "41.733000")
        self.assertEqual(str(pin.location.longitude), "-73.928000")
        self.assertEqual(pin.location.locality, "Poughkeepsie")
        self.assertEqual(pin.location.administrative_area_level_1, "NY")
        self.assertTrue(Wiki.objects.filter(location=pin.location).exists())

    def test_an_unreachable_catalog_still_produces_a_usable_account(self) -> None:
        """A dev environment must not be worth less because REData had nothing to say."""
        summary = self._seed(entries=[])

        owner = User.objects.get(username="demo").profile
        self.assertTrue(summary["created"])
        self.assertEqual(summary["pins"], 1, "the landmark pin, and nothing from the catalog")
        self.assertEqual(summary["landmark"], HUDSON_RIVER_STATE_HOSPITAL["name"])
        self.assertIn("REData", summary["catalog"])
        self.assertTrue(Pin.objects.filter(profile=owner).exists())

    def test_seeding_twice_neither_duplicates_nor_raises(self) -> None:
        self._seed()
        second = self._seed()

        self.assertFalse(second["created"])
        self.assertEqual(User.objects.filter(username="demo").count(), 1)

    def test_it_refuses_to_run_in_production(self) -> None:
        """It writes real coordinates and a password derived from a hostname - not into real data."""
        for environment in ("production", "staging"):
            with (
                self.subTest(environment=environment),
                mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_name", environment),
                pytest.raises(RuntimeError),
            ):
                self._seed()
        self.assertFalse(User.objects.filter(username="demo").exists())


class SeedLandmarkPinTests(TestCase):
    def test_it_is_idempotent_for_one_profile(self) -> None:
        user = User.objects.create_user(username="somebody", password="x")  # noqa: S106 - test fixture

        first = seed_landmark_pin(user.profile)
        second = seed_landmark_pin(user.profile)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Location.objects.filter(latitude="41.733000", longitude="-73.928000").count(), 1)
