"""No GET route may issue more queries as the user's data grows.

A companion to ``test_page_query_budgets.py``, which measures four pages
precisely. This trades precision for breadth: it walks the URL conf, requests
every GET route it can build arguments for, and asserts the query count does not
grow when the dataset does. It deliberately asserts nothing about absolute
counts - a route legitimately costing 40 queries is not this file's business,
while a route costing 40 at 4 pins and 400 at 24 is.

The instrument was validated against a known bug before being trusted. Two
earlier versions of this sweep reported "all routes flat" while being structurally
incapable of seeing the one N+1 known to exist at the time:

1. the first only measured routes reversible with **no arguments**, and the bug
   was on ``label.rows``, which takes a label kind;
2. the second read parameter names from each **leaf** pattern, so every route
   nested under ``path("<str:label_kind>/", include(...))`` - which is where most
   parameterised routes live - appeared to take no arguments and was skipped.

Both produced a confident green. Only after parameters were accumulated down the
resolver tree did reverting the fix light up ``label.rows`` at +80 queries. That
history is the reason ``test_the_sweep_can_see_a_parameterised_route`` exists: a
scaling sweep that has never been shown to catch anything is indistinguishable
from one that cannot.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, get_resolver, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

#: How much a route's query count may grow between the two dataset sizes. Two
#: allows for an extra count/exists query that does not scale, while any genuine
#: per-row query shows up far above it.
_ALLOWED_GROWTH = 2

_SMALL = 4
_LARGE = 24


def _params_of(pattern) -> list[str]:
    regex = getattr(pattern, "regex", None)
    return list(regex.groupindex.keys()) if regex is not None else []


def _walk(patterns, inherited: tuple[str, ...] = (), namespace: str = ""):
    """Yield ``(route_name, params)``, accumulating params *and* namespaces.

    The namespace prefix is as load-bearing as the parameters: routes under an
    included urlconf reverse as ``ns:name``, so yielding the bare local name
    makes every namespaced route - the entire external API - unreversible and
    therefore invisible to the sweep.
    """
    for entry in patterns:
        own = tuple(_params_of(entry.pattern))
        nested = getattr(entry, "url_patterns", None)
        if nested is not None:
            ns = getattr(entry, "namespace", None)
            yield from _walk(nested, inherited + own, f"{namespace}{ns}:" if ns else namespace)
            continue
        name = getattr(entry, "name", None)
        if name:
            yield f"{namespace}{name}", list(dict.fromkeys(inherited + own))


class RouteQueryScalingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)
        self._seeded = 0
        self._headers: dict = {}
        self._seed(_SMALL)
        self.pin = Pin.objects.filter(profile=self.profile).first()
        self.label = Label.objects.filter(profile=self.profile, kind="tag").first()

    def _seed(self, count: int) -> None:
        """Add pins, each with its own Location and a nested label per kind."""
        start = self._seeded
        self._seeded += count
        for i in range(start, start + count):
            pin = baker.make(
                Pin,
                profile=self.profile,
                name=f"Pin {i}",
                location=baker.make(Location, latitude=41.0 + i / 100, longitude=-71.0 - i / 100),
            )
            for kind in ("tag", "category"):
                label = baker.make(Label, profile=self.profile, kind=kind)
                label.parents.add(baker.make(Label, profile=self.profile, kind=kind))
                pin.labels.add(label)

    def _candidates(self) -> dict[str, list]:
        assert self.pin is not None and self.label is not None
        return {
            "label_kind": ["tag", "category", "status"],
            "kind": ["tag", "category", "status"],
            "pin_slug": [self.pin.slug],
            "slug": [self.pin.slug],
            "label_id": [self.label.pk],
            "pk": [self.label.pk],
            "profile_id": [self.profile.pk],
            "username": [self.user.username],
        }

    def _targets(self) -> list[tuple[str, str, str]]:
        """Every GET route this can build a plausible URL for."""
        candidates = self._candidates()
        targets: list[tuple[str, str, str]] = []
        for name, params in _walk(get_resolver().url_patterns):
            if len(params) > 1 or (params and params[0] not in candidates):
                continue
            values = candidates[params[0]] if params else [None]
            for value in values:
                try:
                    url = reverse(name, kwargs={params[0]: value} if params else None)
                except (NoReverseMatch, Exception):
                    continue
                targets.append((name, str(value), url))
        return targets

    def _api_headers(self) -> dict:
        """A bearer key carrying every scope, so API routes answer 200 not 401."""
        from urbanlens.dashboard.models.account.model import ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        api_key, raw = generate_api_key(self.user, "Scaling sweep")
        api_key.scopes = [scope.value for scope in ApiKeyScope]
        api_key.save(update_fields=["scopes"])
        return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def _measure(self, url: str) -> tuple[int, int]:
        """Request *url* inside a savepoint, returning (status, query count).

        The savepoint matters: a route that raises a database error would
        otherwise poison the surrounding test transaction and take every
        subsequent route down with it, turning one broken route into a sweep-wide
        failure that names the wrong culprit.
        """
        with transaction.atomic(), CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url, **self._headers)
            return response.status_code, len(ctx.captured_queries)

    def _sweep(self) -> list[tuple[str, str, int, int]]:
        """Measure every reachable route at both dataset sizes."""
        before: dict[tuple[str, str, str], int] = {}
        for name, value, url in self._targets():
            try:
                status, count = self._measure(url)
            except Exception:
                continue
            if status in (200, 302):
                before[(name, value, url)] = count

        self._seed(_LARGE - _SMALL)

        results = []
        for (name, value, url), small in before.items():
            try:
                _, large = self._measure(url)
            except Exception:
                continue
            results.append((name, value, small, large))
        return results

    def test_the_sweep_reaches_a_useful_number_of_routes(self) -> None:
        """Guards the scaling check from passing by measuring almost nothing."""
        self.assertGreaterEqual(len(self._targets()), 100)

    def test_the_sweep_can_see_a_parameterised_route(self) -> None:
        """Two earlier versions silently skipped these; see the module docstring."""
        names = {name for name, _, _ in self._targets()}

        self.assertIn("label.rows", names, "routes nested under a parameterised include are invisible again")

    def test_the_sweep_can_see_the_namespaced_external_api(self) -> None:
        """Namespaced routes were invisible until ``_walk`` tracked the prefix."""
        names = {name for name, _, _ in self._targets()}

        self.assertTrue(
            any(name.startswith("external_api:") for name in names),
            "the external API is namespaced; without the prefix none of it reverses",
        )

    def test_no_external_api_route_scales(self) -> None:
        """Same property, authenticated with a bearer key rather than a session."""
        self._headers = self._api_headers()
        offenders = [
            f"{name}({value}): {small} -> {large} queries"
            for name, value, small, large in self._sweep()
            if name.startswith("external_api:") and large - small > _ALLOWED_GROWTH
        ]

        self.assertEqual(offenders, [], "these API routes scale with row count:\n  " + "\n  ".join(offenders))

    def test_no_route_issues_more_queries_as_data_grows(self) -> None:
        offenders = [
            f"{name}({value}): {small} -> {large} queries"
            for name, value, small, large in self._sweep()
            if large - small > _ALLOWED_GROWTH
        ]

        self.assertEqual(offenders, [], "these routes scale with row count:\n  " + "\n  ".join(offenders))
