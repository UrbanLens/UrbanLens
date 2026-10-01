"""The route sweep measures what a GET route costs an account, and leaves the database as it found it."""

from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import HttpResponse
from django.test import SimpleTestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.maps import MapController
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.integration_testing import route_costs
from urbanlens.dashboard.services.integration_testing.accounts import prepare_signed_in_account
from urbanlens.dashboard.services.integration_testing.population import population_username
from urbanlens.dashboard.services.integration_testing.route_costs import RouteCost, RouteTarget, SweepResult


def _cost(account: str, name: str, *, ms: float, rows: int, size: int) -> RouteCost:
    return RouteCost(
        account=account,
        name=name,
        url=f"/{name}/",
        status=200,
        wall_ms=(ms, ms, ms),
        sql_n=3,
        sql_rows=rows,
        sql_ms=ms / 2,
        bytes=size,
    )


class TheSweepFindsRoutesTests(SimpleTestCase):
    def test_parameterised_and_namespaced_routes_are_found(self) -> None:
        visited, _ = route_costs.discover_routes()
        by_name = {target.name: target for target in visited}

        self.assertEqual(by_name["pin.details"].params, ("pin_slug",))
        self.assertTrue(any(name.startswith("external_api:") for name in by_name))

    def test_staff_tooling_and_actions_are_never_requested(self) -> None:
        visited, skipped = route_costs.discover_routes()

        self.assertTrue(any(target.route.startswith("admin/") for target in skipped))
        requested = [
            target.name
            for target in visited
            if target.route.startswith(route_costs.SKIPPED_ROUTE_PREFIXES)
            or route_costs.SIDE_EFFECT_NAME.search(target.name)
        ]
        self.assertEqual(requested, [])

    def test_a_url_is_built_only_when_every_parameter_has_a_value(self) -> None:
        target = RouteTarget(name="pin.details", route="", params=("pin_slug",))

        self.assertIsNone(route_costs.build_url(target, {}))
        self.assertEqual(
            route_costs.build_url(target, {"pin_slug": "abandoned-mill"}),
            reverse("pin.details", kwargs={"pin_slug": "abandoned-mill"}),
        )


class TheSweepMeasuresAnAccountTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)

    def test_parameters_come_from_the_accounts_own_rows(self) -> None:
        pin = baker.make(
            Pin, profile=self.profile, name="Mill", location=baker.make(Location, latitude=41.0, longitude=-71.0)
        )
        friend = Profile.objects.get(user=baker.make(User))
        Friendship.objects.create(
            from_profile=friend,
            to_profile=self.profile,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )
        baker.make(Label, profile=self.profile, kind="tag")

        values = route_costs.parameter_values(self.profile)

        pin.refresh_from_db()
        friend.refresh_from_db()
        self.assertEqual(values["pin_slug"], pin.slug)
        self.assertEqual(values["profile_slug"], friend.slug)
        self.assertEqual(values["peer_slug"], friend.slug)
        self.assertTrue(Label.objects.filter(pk=values["label_id"], profile=self.profile, kind="tag").exists())

    def test_what_a_route_writes_is_rolled_back(self) -> None:
        before = Label.objects.count()

        def fetch(url: str) -> HttpResponse:
            baker.make(Label, profile=self.profile, kind="tag")
            return HttpResponse(b"x" * 10)

        cost = route_costs.measure_route(fetch, "/anything/", account="light", name="writes", runs=2)

        self.assertEqual(Label.objects.count(), before)
        self.assertEqual(len(cost.wall_ms), 2)
        self.assertEqual(cost.bytes, 10)
        self.assertGreaterEqual(cost.sql_n, 1)
        self.assertGreaterEqual(cost.sql_rows, 1)

    def test_a_real_route_is_measured_through_the_middleware(self) -> None:
        prepare_signed_in_account(self.user)
        fetch = route_costs.signed_in_fetch(self.user, host=route_costs.request_host())

        cost = route_costs.measure_route(fetch, reverse("map.view"), account="light", name="map.view", runs=1)

        self.assertEqual(cost.status, 200)
        self.assertGreater(cost.bytes, 0)
        self.assertGreater(cost.sql_n, 0)

    def test_a_route_that_raises_is_a_500_rather_than_the_end_of_the_sweep(self) -> None:
        prepare_signed_in_account(self.user)
        url = reverse("map.view")
        fetch = route_costs.signed_in_fetch(self.user, host=route_costs.request_host())

        with mock.patch.object(MapController, "view_map", side_effect=RuntimeError("fell over")):
            cost = route_costs.measure_route(fetch, url, account="light", name="map.view", runs=1)

        self.assertEqual(cost.status, 500)


class TheReportTests(SimpleTestCase):
    def test_a_route_whose_cost_grows_with_the_account_is_flagged_and_listed_first(self) -> None:
        result = SweepResult(runs=3, accounts={"light": 12, "heavy": 20_000})
        result.costs = [
            _cost("light", "map.search", ms=20, rows=12, size=2_000),
            _cost("heavy", "map.search", ms=4_000, rows=20_000, size=9_000_000),
            _cost("light", "profile.view", ms=40, rows=30, size=20_000),
            _cost("heavy", "profile.view", ms=45, rows=31, size=20_500),
        ]
        result.unfilled = {"light": ["trips.detail"], "heavy": ["trips.detail"]}

        lines = route_costs.render(result, light="light", heavy="heavy").splitlines()

        search = next(line for line in lines if line.startswith("| map.search |"))
        profile = next(line for line in lines if line.startswith("| profile.view |"))
        self.assertIn("**grows**", search)
        self.assertNotIn("**grows**", profile)
        self.assertLess(lines.index(search), lines.index(profile))
        self.assertTrue(any("trips.detail" in line for line in lines))

    def test_growth_needs_a_ratio_and_a_floor(self) -> None:
        light = _cost("light", "r", ms=1, rows=1, size=100)

        self.assertFalse(route_costs.grows(light, _cost("heavy", "r", ms=100, rows=500, size=10_000)))
        self.assertTrue(route_costs.grows(light, _cost("heavy", "r", ms=1, rows=route_costs.MANY_ROWS, size=100)))


class TheCommandTests(TestCase):
    def _population_account(self) -> None:
        profile = prepare_signed_in_account(baker.make(User, username=population_username(0)))
        baker.make(Pin, profile=profile, name="Mill", location=baker.make(Location, latitude=41.0, longitude=-71.0))

    def test_production_is_refused(self) -> None:
        self._population_account()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("urbanlens.dashboard.services.integration_testing.guards.app_settings") as app_settings,
        ):
            app_settings.environment_name = "production"
            with self.assertRaisesMessage(CommandError, "production"):
                call_command(
                    "measure_route_costs",
                    "--out",
                    str(Path(directory) / "costs.json"),
                    stdout=StringIO(),
                    stderr=StringIO(),
                )

    def test_without_a_population_there_is_nothing_to_measure(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesMessage(CommandError, "--population"):
            call_command(
                "measure_route_costs",
                "--out",
                str(Path(directory) / "costs.json"),
                stdout=StringIO(),
                stderr=StringIO(),
            )

    def test_a_sweep_writes_its_measurements_and_its_report(self) -> None:
        self._population_account()

        with tempfile.TemporaryDirectory() as directory:
            out, markdown = Path(directory) / "costs.json", Path(directory) / "costs.md"
            call_command(
                "measure_route_costs",
                "--out",
                str(out),
                "--markdown",
                str(markdown),
                "--runs",
                "1",
                "--only",
                r"^map\.view$",
                stdout=StringIO(),
                stderr=StringIO(),
            )
            measured = json.loads(out.read_text(encoding="utf-8"))
            report = markdown.read_text(encoding="utf-8")

        self.assertEqual(
            {(cost["account"], cost["name"], cost["status"]) for cost in measured["costs"]},
            {("light", "map.view", 200), ("heavy", "map.view", 200)},
        )
        self.assertIn("| map.view |", report)
