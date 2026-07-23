"""Tests for the External APIs toggle (Profile.external_apis_enabled).

Covers the panel-fetch chokepoint (schedule_panel_fetch), the AI gateway
factory's centralized per-profile check, and the weather endpoint - the three
representative call sites for the master switch.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.test import RequestFactory
from django.urls import reverse
import pytest

from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.controllers.pin import PinController
from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.external_data import schedule_panel_fetch

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


@pytest.mark.django_db
def test_get_gateway_returns_none_when_profile_external_apis_disabled() -> None:
    profile = _make_profile(ai_enabled=True, external_apis_enabled=False)

    assert get_gateway(profile=profile) is None


@pytest.mark.django_db
def test_get_gateway_returns_none_when_profile_ai_disabled() -> None:
    profile = _make_profile(ai_enabled=False, external_apis_enabled=True)

    assert get_gateway(profile=profile) is None


@pytest.mark.django_db
def test_get_gateway_allows_when_profile_fully_enabled() -> None:
    profile = _make_profile(ai_enabled=True, external_apis_enabled=True)

    with mock.patch("urbanlens.dashboard.services.ai.cloudflare.CloudflareGateway") as gateway_cls:
        get_gateway(profile=profile)

    gateway_cls.assert_called_once()


@pytest.mark.django_db
def test_get_gateway_ignores_profile_check_when_no_profile_given() -> None:
    """Site-wide/no-profile callers (e.g. admin tooling) are unaffected."""
    with mock.patch("urbanlens.dashboard.services.ai.cloudflare.CloudflareGateway") as gateway_cls:
        get_gateway()

    gateway_cls.assert_called_once()


@pytest.mark.django_db
def test_schedule_panel_fetch_skipped_when_external_apis_disabled() -> None:
    from model_bakery import baker

    pin: Pin = baker.make_recipe("dashboard.pin")
    pin.profile.external_apis_enabled = False
    pin.profile.save(update_fields=["external_apis_enabled"])

    with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
        result = schedule_panel_fetch("boundary", pin)

    assert result is False
    fetch_task.apply_async.assert_not_called()


@pytest.mark.django_db
def test_schedule_panel_fetch_runs_when_external_apis_enabled() -> None:
    from model_bakery import baker

    pin: Pin = baker.make_recipe("dashboard.pin")
    assert pin.profile.external_apis_enabled

    with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
        result = schedule_panel_fetch("boundary", pin)

    assert result is True
    fetch_task.apply_async.assert_called_once_with(args=["boundary", pin.pk], queue="celery")


@pytest.mark.django_db
def test_fast_panel_dispatches_to_the_panel_fetch_queue() -> None:
    """Most panels (pure HTTP + JSON, no CPU-heavy parsing) default to the
    high-concurrency thread-pool queue - see PanelSource.queue and
    docker-compose.yml's celery-worker-panels service."""
    from model_bakery import baker

    pin: Pin = baker.make_recipe("dashboard.pin")

    with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
        schedule_panel_fetch("photon", pin)

    fetch_task.apply_async.assert_called_once_with(args=["photon", pin.pk], queue="panel_fetch")


@pytest.mark.django_db
def test_cpu_heavy_panels_stay_on_the_default_queue() -> None:
    """BoundaryPanelSource and OvertureBuildingAttributesPanelSource do real CPU-bound
    work (gunzipping/parsing GeoParquet, shapely geometry) - several of those running
    concurrently on the thread-pool queue would cause enough GIL contention to slow
    down every other panel sharing it, so they opt out via PanelSource.queue."""
    from urbanlens.dashboard.services.external_data import BoundaryPanelSource, get_panel_source

    assert BoundaryPanelSource().queue == "celery"

    overture = get_panel_source("overture_building_attributes")
    assert overture is not None
    assert overture.queue == "celery"


@pytest.mark.django_db
def test_weather_forecast_blocked_when_external_apis_disabled() -> None:
    from model_bakery import baker

    pin: Pin = baker.make_recipe("dashboard.pin")
    pin.profile.external_apis_enabled = False
    pin.profile.save(update_fields=["external_apis_enabled"])

    request = RequestFactory().get(reverse("pin.weather_forecast", args=[pin.slug]))
    request.user = pin.profile.user

    response = PinController.as_view({"get": "weather_forecast"})(request, pin_slug=pin.slug)

    assert response.status_code == 403


@pytest.mark.django_db
def test_streetview_check_blocked_when_external_apis_disabled() -> None:
    """Regression: the map's right-click Street View probe called Google (with
    the clicked coordinates) even for profiles that opted out of external
    lookups - it must short-circuit before ever building the API request."""
    import json

    from urbanlens.dashboard.controllers import maps as maps_module

    profile = _make_profile(external_apis_enabled=False)
    request = RequestFactory().get(reverse("map.streetview_check"), {"lat": "40.7", "lng": "-74.0"})
    request.user = profile.user

    with mock.patch.object(maps_module.urllib.request, "urlopen") as mocked_urlopen:
        response = maps_module.MapController.as_view({"get": "streetview_check"})(request)

    mocked_urlopen.assert_not_called()
    assert json.loads(response.content) == {"available": False, "reason": "disabled"}
