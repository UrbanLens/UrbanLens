"""A key REData refuses for an endpoint is a configuration fault: reported once, then left alone for a long while.

REData answers 403 when the key lacks the endpoint's scope. Treated as an outage, every pin's panel asked again
every five minutes and logged a warning each time, for as long as the key stayed unchanged.
"""

from __future__ import annotations

import json
import logging
from unittest import mock

from django.core.cache import cache
import pytest

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import (
    LocationContextUnavailableError,
    RedataLocationContextGateway,
)
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.core.gateway import UpstreamBusyError, is_source_outage
from urbanlens.dashboard.services.core.rate_limiter import UpstreamThrottledError, _RateLimitedSession
from urbanlens.dashboard.services.core.upstream_breaker import RedataBreaker

_BASE = "https://redata.example.test/api/v1/"
_HAZARDS = f"{_BASE}hazards/"
_INCIDENTS = f"{_BASE}incidents/"
_OWNERS = f"{_BASE}parcels/3fae2b1c-0000-0000-0000-000000000000/owners/"
_REFUSED = {"detail": "You do not have permission to perform this action."}

#: An hour is the floor: anything shorter is the five-minute loop again with a longer period.
_LONG = 3600


def _response(status_code: int, body: dict | None = None) -> mock.Mock:
    response = mock.Mock(status_code=status_code, headers={}, ok=200 <= status_code < 300)
    response.json.return_value = body or {}
    response.text = json.dumps(body or {})
    return response


def _session(service: str, *responses: mock.Mock) -> _RateLimitedSession:
    session = _RateLimitedSession(service)
    session._session = mock.Mock()
    session._session.request.side_effect = list(responses)
    return session


class RefusedEndpointBreakerTests(TestCase):
    def test_a_403_stops_every_call_to_that_endpoint_whatever_its_providers(self) -> None:
        _session("redata_hazards", _response(403, _REFUSED)).get(_HAZARDS, params={"provider": "usgs_earthquakes"})
        hazards = _session("redata_hazards")

        with pytest.raises(UpstreamThrottledError) as caught:
            hazards.get(_HAZARDS, params={"provider": ["nifc_wildfires", "fema_disasters"]})

        hazards._session.request.assert_not_called()
        self.assertGreaterEqual(caught.value.retry_after, _LONG - 1)

    def test_a_403_leaves_other_endpoints_callable(self) -> None:
        _session("redata_hazards", _response(403, _REFUSED)).get(_HAZARDS)
        incidents = _session("redata_incidents", _response(200, {"count": 0, "results": []}))

        incidents.get(_INCIDENTS)

        incidents._session.request.assert_called_once()

    def test_a_refusal_on_one_parcel_covers_every_parcel(self) -> None:
        _session("redata_api", _response(403, _REFUSED)).get(_OWNERS)
        api = _session("redata_api")

        with pytest.raises(UpstreamThrottledError):
            api.get(f"{_BASE}parcels/0b6a51d2-0000-0000-0000-000000000000/owners/")

    def test_the_refusal_is_reported_once_however_often_it_recurs(self) -> None:
        with self.assertLogs("urbanlens.dashboard.services.core.upstream_breaker", level=logging.INFO) as logs:
            _session("redata_hazards", _response(403, _REFUSED)).get(_HAZARDS)
            # The breaker's own wait elapsing, and the key still lacking the scope.
            cache.delete(RedataBreaker()._key("refused:hazards"))
            _session("redata_hazards", _response(403, _REFUSED)).get(_HAZARDS)

        self.assertEqual(len([record for record in logs.records if record.levelno >= logging.ERROR]), 1)

    def test_the_refused_endpoints_are_listed_for_the_site_admin(self) -> None:
        _session("redata_hazards", _response(403, _REFUSED)).get(_HAZARDS)
        _session("redata_api", _response(403, _REFUSED)).get(_OWNERS)

        refused = {entry.endpoint for entry in RedataBreaker().refused_endpoints()}

        self.assertEqual(refused, {"hazards", "parcels/{id}/owners"})


class RefusalIsBusyNotAnOutageTests(TestCase):
    """The first refusal already backs the caller off for as long as the breaker will."""

    def test_the_location_context_gateway_backs_off_long(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(403, _REFUSED)
        gateway = RedataLocationContextGateway(base_url="https://redata.example.test", api_key="k", session=session)

        with pytest.raises(LocationContextUnavailableError) as caught:
            gateway.near_point("/api/v1/hazards/", 41.7, -73.9)

        self.assertIsInstance(caught.value, UpstreamBusyError)
        self.assertGreaterEqual(caught.value.retry_after, _LONG)
        self.assertTrue(is_source_outage(caught.value), "a refusal says nothing about the place, so nothing is cached")

    def test_the_property_records_gateway_backs_off_long(self) -> None:
        session = mock.Mock()
        session.get.return_value = _response(403, _REFUSED)
        gateway = RedataGateway(base_url="https://redata.example.test", api_key="k", session=session)

        with pytest.raises(PropertyRecordsUnavailableError) as caught:
            gateway.lookup_owners("3fae2b1c-0000-0000-0000-000000000000")

        self.assertIsInstance(caught.value, UpstreamBusyError)
        self.assertGreaterEqual(caught.value.retry_after, _LONG)
        self.assertTrue(caught.value.is_outage)


class RefusalsOnTheApiLimitsPageTests(TestCase):
    def test_the_site_admin_sees_the_refused_endpoint(self) -> None:
        from django.contrib.auth.models import User
        from django.test import Client
        from django.urls import reverse
        from model_bakery import baker

        from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

        _session("redata_hazards", _response(403, _REFUSED)).get(_HAZARDS)
        admin = baker.make(User)
        add_user_to_site_admin_group(admin)
        client = Client()
        client.force_login(admin)

        response = client.get(reverse("site_admin_api_limits"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([refusal["endpoint"] for refusal in response.context["redata_refusals"]], ["hazards"])
