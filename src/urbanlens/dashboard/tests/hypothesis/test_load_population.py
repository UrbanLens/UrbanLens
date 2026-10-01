"""The capacity test's population must be what its manifest says, and its sessions must be real sign-ins."""

from __future__ import annotations

from io import StringIO
import json
import math
from pathlib import Path
import stat
import tempfile
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import Status
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.integration_testing.accounts import integration_users
from urbanlens.dashboard.services.integration_testing.perf_seed import COORDINATE_STEP, GRID_SIDE
from urbanlens.dashboard.services.integration_testing.population import (
    ACCOUNT_LATITUDE_STEP,
    DEFAULT_TIERS,
    FRIENDS_AHEAD,
    NOTIFICATIONS_PER_ACCOUNT,
    UNREAD_MESSAGES,
    UNREAD_NOTIFICATIONS,
    SizeTier,
    origin_for,
    pins_for,
    population_username,
    provision_population,
)

#: Every account the same small size, so a test pays for rows it asserts on and no more.
TINY = (SizeTier(share=1.0, min_pins=12, max_pins=12),)


class TheSizeDistributionTests(TestCase):
    """Sizes are a fixture: reproducible, bounded, and distributed the way the tiers say."""

    def test_an_index_always_gets_the_same_size(self) -> None:
        self.assertEqual([pins_for(index) for index in range(50)], [pins_for(index) for index in range(50)])

    def test_every_size_falls_inside_a_tier(self) -> None:
        for index in range(2_000):
            size = pins_for(index)
            self.assertTrue(
                any(tier.min_pins <= size <= tier.max_pins for tier in DEFAULT_TIERS), f"index {index} drew {size}"
            )

    def test_the_tiers_get_their_share(self) -> None:
        """Counted per tier over a large sample; a skewed draw would make "1,000 users" mean a different site."""
        sample = 10_000
        sizes = [pins_for(index) for index in range(sample)]
        largest = DEFAULT_TIERS[-1]

        in_largest = sum(1 for size in sizes if size >= largest.min_pins)

        self.assertAlmostEqual(in_largest / sample, largest.share, delta=0.005)
        smallest = DEFAULT_TIERS[0]
        in_smallest = sum(1 for size in sizes if size < smallest.max_pins)
        self.assertAlmostEqual(in_smallest / sample, smallest.share, delta=0.02)

    def test_the_largest_account_fits_inside_one_row_of_the_lattice(self) -> None:
        tallest = math.ceil(max(tier.max_pins for tier in DEFAULT_TIERS) / GRID_SIDE) * COORDINATE_STEP

        self.assertLess(tallest, ACCOUNT_LATITUDE_STEP)

    def test_tiers_whose_shares_do_not_cover_everyone_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            pins_for(0, (SizeTier(share=0.5, min_pins=1, max_pins=2),))

    def test_a_tier_that_would_overlap_the_next_row_is_refused(self) -> None:
        huge = (SizeTier(share=1.0, min_pins=1, max_pins=40_000),)

        with self.assertRaises(ValueError):
            provision_population(1, tiers=huge)

        self.assertFalse(User.objects.filter(username=population_username(0)).exists())

    def test_neighbouring_accounts_are_placed_apart_but_overlapping(self) -> None:
        first, second = origin_for(0), origin_for(1)

        self.assertEqual(first[0], second[0])
        self.assertLess(second[1] - first[1], GRID_SIDE * COORDINATE_STEP)


class ThePopulationIsWhatTheManifestSaysTests(TestCase):
    """Every count the harness relies on must be a count in the database."""

    def test_every_account_is_one_purge_selects(self) -> None:
        result = provision_population(3, tiers=TINY)

        self.assertEqual(
            {account.username for account in result.accounts}, {user.username for user in integration_users()}
        )

    def test_each_account_holds_the_pins_it_reports(self) -> None:
        result = provision_population(3, tiers=TINY)

        for account in result.accounts:
            self.assertEqual(
                Pin.objects.filter(profile__user__username=account.username).root_pins().count(), account.pins
            )
        self.assertEqual({account.pins for account in result.accounts}, {12})

    def test_accounts_are_friends_with_the_ones_after_them_and_no_further(self) -> None:
        result = provision_population(FRIENDS_AHEAD + 2, tiers=TINY)
        profiles = [User.objects.get(username=account.username).profile for account in result.accounts]

        def befriended(a, b) -> bool:
            return Friendship.objects.filter(from_profile__in=(a, b), to_profile__in=(a, b)).exists()

        self.assertTrue(befriended(profiles[0], profiles[1]))
        self.assertTrue(befriended(profiles[0], profiles[FRIENDS_AHEAD]))
        self.assertFalse(befriended(profiles[0], profiles[FRIENDS_AHEAD + 1]))

    def test_neighbours_have_a_conversation_ending_unread(self) -> None:
        result = provision_population(2, tiers=TINY)
        first, second = (User.objects.get(username=account.username).profile for account in result.accounts)

        between = DirectMessage.objects.filter(sender__in=(first, second), recipient__in=(first, second))

        self.assertGreater(between.count(), 0)
        self.assertEqual(between.filter(read_at__isnull=True).count(), UNREAD_MESSAGES)

    def test_every_account_has_unread_notifications(self) -> None:
        result = provision_population(2, tiers=TINY)

        for account in result.accounts:
            logs = NotificationLog.objects.filter(profile__user__username=account.username)
            self.assertEqual(logs.count(), NOTIFICATIONS_PER_ACCOUNT)
            self.assertEqual(logs.filter(status=Status.UNREAD).count(), UNREAD_NOTIFICATIONS)

    def test_visits_are_logged(self) -> None:
        result = provision_population(1, tiers=TINY)

        self.assertGreater(PinVisit.objects.filter(pin__profile__user__username=result.accounts[0].username).count(), 0)

    def test_a_second_run_adds_nothing_but_new_sessions(self) -> None:
        first = provision_population(3, tiers=TINY)
        counts = (
            Pin.objects.count(),
            Friendship.objects.count(),
            DirectMessage.objects.count(),
            NotificationLog.objects.count(),
            PinVisit.objects.count(),
        )

        second = provision_population(3, tiers=TINY)

        self.assertEqual(
            (
                Pin.objects.count(),
                Friendship.objects.count(),
                DirectMessage.objects.count(),
                NotificationLog.objects.count(),
                PinVisit.objects.count(),
            ),
            counts,
        )
        self.assertEqual(second.created, 0)
        self.assertEqual(second.pins_created, 0)
        self.assertNotEqual(first.accounts[0].cookies, second.accounts[0].cookies)

    def test_the_manifest_carries_sessions_and_no_password(self) -> None:
        manifest = provision_population(2, tiers=TINY).manifest(site_url="http://testserver", environment="test")

        document = json.dumps(manifest)

        self.assertNotIn("password", document)
        self.assertEqual(manifest["pins"], 24)
        for account in manifest["accounts"]:
            self.assertEqual(set(account["cookies"]), {settings.SESSION_COOKIE_NAME, settings.CSRF_COOKIE_NAME})


class TheMintedSessionIsARealSignInTests(TestCase):
    """A minted session that bounced to the sign-in page would measure the sign-in page."""

    def setUp(self) -> None:
        super().setUp()
        self.result = provision_population(2, tiers=TINY)
        self.manifest = self.result.manifest(site_url="http://testserver", environment="test")

    def _client_for(self, index: int) -> Client:
        client = Client(enforce_csrf_checks=True)
        for name, value in self.result.accounts[index].cookies.items():
            client.cookies[name] = value
        return client

    def test_the_session_reaches_the_map_as_its_own_account(self) -> None:
        response = self._client_for(0).get(self.manifest["routes"]["map.view"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.username, self.result.accounts[0].username)

    def test_another_accounts_session_is_that_account(self) -> None:
        response = self._client_for(1).get(self.manifest["routes"]["map.view"])

        self.assertEqual(response.wsgi_request.user.username, self.result.accounts[1].username)

    def test_every_account_path_answers_signed_in(self) -> None:
        client = self._client_for(0)

        for key, path in self.result.accounts[0].paths.items():
            with self.subTest(key=key, path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.wsgi_request.user.is_authenticated)

    def test_a_post_carrying_the_minted_token_passes_csrf(self) -> None:
        account = self.result.accounts[0]

        response = self._client_for(0).post(
            self.manifest["routes"]["map.search"],
            {"name": "Perf Pin"},
            HTTP_X_CSRFTOKEN=account.cookies[settings.CSRF_COOKIE_NAME],
        )

        self.assertNotEqual(response.status_code, 403)

    def test_a_post_without_the_token_is_still_refused(self) -> None:
        """The negative half: proves the client above really enforces CSRF."""
        response = self._client_for(0).post(self.manifest["routes"]["map.search"], {"name": "Perf Pin"})

        self.assertEqual(response.status_code, 403)


class ThePopulationCommandTests(TestCase):
    """The command writes sessions only where they are private, and never in production."""

    def test_the_manifest_is_written_readable_by_its_owner_alone(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("urbanlens.dashboard.services.integration_testing.population.DEFAULT_TIERS", TINY),
        ):
            out = Path(directory) / "population.json"
            call_command(
                "provision_integration_env",
                "--population",
                "2",
                "--out",
                str(out),
                stdout=StringIO(),
                stderr=StringIO(),
            )

            manifest = json.loads(out.read_text(encoding="utf-8"))
            mode = stat.S_IMODE(out.stat().st_mode)

        self.assertEqual(manifest["kind"], "population")
        self.assertEqual(len(manifest["accounts"]), 2)
        self.assertEqual(mode, 0o600)

    def test_a_population_is_never_printed(self) -> None:
        with self.assertRaises(CommandError):
            call_command("provision_integration_env", "--population", "2", stdout=StringIO(), stderr=StringIO())

        self.assertFalse(User.objects.filter(username=population_username(0)).exists())

    def test_production_is_refused_for_a_population(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("urbanlens.dashboard.services.integration_testing.guards.app_settings") as app_settings,
        ):
            app_settings.environment_name = "production"
            with self.assertRaises(CommandError):
                call_command(
                    "provision_integration_env",
                    "--population",
                    "1",
                    "--out",
                    str(Path(directory) / "p.json"),
                    stdout=StringIO(),
                    stderr=StringIO(),
                )

        self.assertFalse(User.objects.filter(username=population_username(0)).exists())
