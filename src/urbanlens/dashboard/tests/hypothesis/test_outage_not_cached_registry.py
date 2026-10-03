"""Every source that caches to LocationCache, run while its upstream cannot be reached, writes nothing.

The sources are read from the panel and enrichment registries, so one added later is held to this without being
listed here. The upstream is cut off below every gateway, at the socket (or, for a 503, at requests' adapter), which is
where production's egress refusal happened.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager, suppress
from datetime import timedelta
import errno
import json
import socket
from typing import TYPE_CHECKING, Any
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import caches
from django.utils import timezone
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.google_place.model import GooglePlace
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from urbanlens.dashboard.services.locations.enrichment import EnrichmentSource

#: Sources that ask no upstream for the fixture, so an outage cannot reach them, and why.
_NO_UPSTREAM: dict[str, str] = {
    "wikipedia_media": "asks for the images of the article the wikipedia row names, and the outage left none",
}

#: Panel sources whose gate cannot pass for the fixture pin, and why.
_GATED_OUT: dict[str, str] = {
    "epa_echo_detail": "only for a pin whose Location already matched an ECHO facility",
}

#: Enrichment sources the batch would not pick the fixture Location for, and why.
_NOT_A_CANDIDATE: dict[str, str] = {
    "google_place_link": "picks only unlinked Locations, the fixture needs its link for a CID, and it keeps no row",
}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
_STALE = timedelta(days=400)


def _is_local(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    return str(host or "").strip().lower() in _LOCAL_HOSTS


class _Upstream:
    """Cuts every non-local connection off one way, counting the attempts."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.attempts = 0

    def _refusal(self) -> OSError:
        self.attempts += 1
        if self.mode == "timeout":
            return TimeoutError("timed out")
        return ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

    @contextmanager
    def cut(self) -> Iterator[_Upstream]:
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect
        real_create_connection = socket.create_connection

        def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            if _is_local(host):
                return real_getaddrinfo(host, port, *args, **kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", int(port or 443)))]

        def connect(sock: socket.socket, address: Any) -> Any:
            if _is_local(address[0] if isinstance(address, tuple) and address else None):
                return real_connect(sock, address)
            raise self._refusal()

        def connect_ex(sock: socket.socket, address: Any) -> int:
            connect(sock, address)
            return 0

        def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
            if _is_local(address[0]):
                return real_create_connection(address, *args, **kwargs)
            raise self._refusal()

        def unavailable(
            adapter: Any, request: requests.PreparedRequest, *args: Any, **kwargs: Any
        ) -> requests.Response:
            self.attempts += 1
            response = requests.Response()
            response.status_code = 503
            response.reason = "Service Unavailable"
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps({"error": "source_error", "message": "upstream unavailable"}).encode()
            response.url = request.url or ""
            response.request = request
            return response

        with ExitStack() as stack:
            stack.enter_context(mock.patch("socket.getaddrinfo", getaddrinfo))
            stack.enter_context(mock.patch.object(socket.socket, "connect", connect))
            stack.enter_context(mock.patch.object(socket.socket, "connect_ex", connect_ex))
            stack.enter_context(mock.patch("socket.create_connection", create_connection))
            if self.mode == "503":
                stack.enter_context(mock.patch.object(requests.adapters.HTTPAdapter, "send", unavailable))
            yield self


def _snapshot(location: Location) -> set[tuple[str, str, str, str]]:
    rows = LocationCache.objects.filter(location=location)
    return {
        (row.source, row.audience, json.dumps(row.data, sort_keys=True, default=str), row.updated.isoformat())
        for row in rows
    }


def _written(before: set[tuple[str, str, str, str]], after: set[tuple[str, str, str, str]]) -> list[str]:
    return sorted(f"{source}[{audience[:8]}] = {data[:100]}" for source, audience, data, _updated in after - before)


class _OutageFixture(RedataConfiguredMixin, TestCase):
    """A New York site every registered source has something to ask about."""

    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        for attribute in ("azure_maps_subscription_key", "google_unrestricted_api_key"):
            patcher = mock.patch.object(app_settings, attribute, "test-key")
            patcher.start()
            self.addCleanup(patcher.stop)
        # State outlines are fetched from TIGERweb, which the test network refuses; every region-gated source should run.
        inside = mock.patch.object(GeoBoundary, "contains", return_value=True)
        inside.start()
        self.addCleanup(inside.stop)
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.profile = baker.make(User).profile
        google_place = GooglePlace.objects.create(latitude="41.7321", longitude="-73.9262", cid=123456789012345678)
        self.location = baker.make(
            Location,
            latitude="41.7321",
            longitude="-73.9262",
            official_name="Hudson River State Hospital",
            official_name_source="google_places",
            street_number="2400",
            route="Route 9",
            locality="Poughkeepsie",
            administrative_area_level_1="NY",
            zipcode="12601",
            country="US",
            google_place=google_place,
        )
        self.pin = baker.make(
            Pin, profile=self.profile, location=self.location, parent_pin=None, name="Hudson River Psychiatric Center"
        )

    def _run_as_the_worker_does(self, run: Callable[[], object]) -> None:
        """A raise is that one source's failure, and whatever it wrote first stays written.

        Each source starts with its own quota: an earlier one's attempts would otherwise use up a shared per-minute
        limit, and a refusal by the limiter asks nothing.
        """
        for alias in caches:
            caches[alias].clear()
        ApiCallLog.objects.all().delete()
        with suppress(Exception):
            run()


class PanelSourceOutageTests(_OutageFixture):
    """Each panel through the worker's own entry point, :func:`run_panel_fetch`, over a stale good row."""

    def _sweep(self, mode: str) -> None:
        from urbanlens.dashboard.services.pins.external_data import (
            LocationCachePanelSource,
            gate_allows,
            panel_sources,
            run_panel_fetch,
        )

        sources = [source for source in panel_sources().values() if isinstance(source, LocationCachePanelSource)]
        self.assertGreater(len(sources), 30, "the registry did not load, so nothing below is being tested")
        unexercised: dict[str, str] = {}
        leaked: dict[str, list] = {}
        for source in sources:
            if source.key in _GATED_OUT:
                continue
            if not gate_allows(source, self.pin):
                unexercised[source.key] = "gate refused the fixture pin"
                continue
            for scope in source.search_scopes(self.pin):
                row = LocationCache.set(
                    self.location,
                    source.cache_source,
                    {"good": "kept", "items": [{"url": "https://example.test/a"}]},
                    query_key="earlier",
                    audience=scope.audience,
                )
                LocationCache.objects.filter(pk=row.pk).update(updated=timezone.now() - _STALE)
            before = _snapshot(self.location)
            upstream = _Upstream(mode)
            with upstream.cut():
                self._run_as_the_worker_does(lambda source=source: run_panel_fetch(source.key, self.pin))
            after = _snapshot(self.location)
            if upstream.attempts == 0 and source.key not in _NO_UPSTREAM:
                unexercised[source.key] = "asked no upstream"
            if before != after:
                leaked[source.key] = _written(before, after)
            LocationCache.objects.filter(location=self.location).delete()

        # Leaked: wrote during an outage, and nothing refetches a source that has a fresh row.
        # Unexercised: the sweep proves nothing about these.
        self.assertEqual({"leaked": leaked, "unexercised": unexercised}, {"leaked": {}, "unexercised": {}})

    def test_connection_refused(self) -> None:
        self._sweep("refused")

    def test_timeout(self) -> None:
        self._sweep("timeout")

    def test_service_unavailable(self) -> None:
        self._sweep("503")


def _enrichment_sources() -> list[EnrichmentSource]:
    from urbanlens.dashboard.services.locations.enrichment import enrichment_sources

    return enrichment_sources()


class EnrichmentSourceOutageTests(_OutageFixture):
    """The background batch: each source's ``enrich`` for a Location it has never filled."""

    def setUp(self) -> None:
        super().setUp()
        # Unaddressed, as the address backfill's candidates are.
        Location.objects.filter(pk=self.location.pk).update(street_number=None, route=None)
        self.location.refresh_from_db()

    def _candidates(self) -> list[EnrichmentSource]:
        sources = _enrichment_sources()
        self.assertGreater(len(sources), 5, "the registry did not load, so nothing below is being tested")
        return [source for source in sources if source.key not in _NOT_A_CANDIDATE]

    def _picks_the_fixture(self, source: EnrichmentSource) -> bool:
        return Location.objects.filter(source.missing_filter(), pk=self.location.pk).exists()

    def _sweep(self, mode: str) -> None:
        unexercised: dict[str, str] = {}
        leaked: dict[str, list] = {}
        for source in self._candidates():
            LocationCache.objects.filter(location=self.location).delete()
            if not source.gate():
                unexercised[source.key] = "gate refused"
                continue
            if not self._picks_the_fixture(source):
                unexercised[source.key] = "the batch would not pick the fixture Location"
                continue
            before = _snapshot(self.location)
            upstream = _Upstream(mode)
            with upstream.cut():
                self._run_as_the_worker_does(lambda source=source: source.enrich(self.location))
            after = _snapshot(self.location)
            if upstream.attempts == 0 and source.key not in _NO_UPSTREAM:
                unexercised[source.key] = "asked no upstream"
            if before != after:
                leaked[source.key] = _written(before, after)

        # Leaked: wrote during an outage, and the batch never revisits a Location with a row.
        self.assertEqual({"leaked": leaked, "unexercised": unexercised}, {"leaked": {}, "unexercised": {}})

    def test_connection_refused(self) -> None:
        self._sweep("refused")

    def test_timeout(self) -> None:
        self._sweep("timeout")

    def test_service_unavailable(self) -> None:
        self._sweep("503")

    def test_one_batch_in_registry_order(self) -> None:
        """As the batch runs them: a source reading another's row must not turn that source's outage into its own."""
        upstream = _Upstream("refused")
        with upstream.cut():
            for source in self._candidates():
                if source.gate() and self._picks_the_fixture(source):
                    self._run_as_the_worker_does(lambda source=source: source.enrich(self.location))

        self.assertGreater(upstream.attempts, 0)
        self.assertEqual(_written(set(), _snapshot(self.location)), [])


class ExemptionTests(TestCase):
    def test_every_exemption_names_a_registered_source(self) -> None:
        """An exemption for a source that no longer exists would hide the next one to take its key."""
        from urbanlens.dashboard.services.pins.external_data import panel_sources

        registered = set(panel_sources()) | {source.key for source in _enrichment_sources()}
        self.assertLessEqual(set(_GATED_OUT) | set(_NO_UPSTREAM) | set(_NOT_A_CANDIDATE), registered)
