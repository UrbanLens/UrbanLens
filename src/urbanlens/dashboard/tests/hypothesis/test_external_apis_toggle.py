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
    fetch_task.delay.assert_not_called()


@pytest.mark.django_db
def test_schedule_panel_fetch_runs_when_external_apis_enabled() -> None:
    from model_bakery import baker

    pin: Pin = baker.make_recipe("dashboard.pin")
    assert pin.profile.external_apis_enabled

    with mock.patch("urbanlens.dashboard.tasks.fetch_panel_source") as fetch_task:
        result = schedule_panel_fetch("boundary", pin)

    assert result is True
    fetch_task.delay.assert_called_once()


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
