"""Tests for the external API's pin-detail panel domain.

Two kinds of coverage are mixed deliberately:

1. Tests against the **real** panel registry, guarding the product decision
   documented in ``services.external_data`` and ``docs/notes/mobile_app_notes.md``
   (D8): the satellite/street-view carousels stay off this API forever until a
   signed slide-image proxy exists, because their payload is base64 imagery and
   this API's throttle counts requests, not bytes.
2. Tests against a **stub** source with ``panel_sources``/``get_panel_source``
   patched, for full control over the ready / not-ready / gated / hidden
   permutations without depending on any specific plugin's fetch behavior.
"""

from __future__ import annotations

from typing import ClassVar
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.external_api import views_panels
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.external_data import POLL_INTERVAL_SECONDS, PanelApiKind, PanelSource


class _StubPanelSource(PanelSource):
    """A fully-controllable stub source for panels-endpoint tests."""

    key = "stub_panel"
    api_kinds: ClassVar[frozenset[PanelApiKind]] = frozenset({PanelApiKind.INFO})

    def __init__(self, *, ready: bool = True, gated: bool = True, payload: dict | None = None) -> None:
        self._ready = ready
        self._gated = gated
        self._payload = payload

    def is_ready(self, pin: Pin) -> bool:
        return self._ready

    def gate(self, pin: Pin) -> bool:
        return self._gated

    def fetch(self, pin: Pin) -> None:
        """No-op: this stub exists to test the API contract, not fetching."""

    def api_payload(self, pin: Pin) -> dict | None:
        return self._payload


class _FeatureGatedStubPanelSource(_StubPanelSource):
    """Same stub, gated behind a SiteFeature - for feature-visibility tests."""

    required_feature: ClassVar[SiteFeature] = SiteFeature.NEARBY_RESEARCH


def _bearer(raw_key: str) -> dict:
    """Request kwargs carrying an API key as a bearer token."""
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class _PanelsApiTestCase(TestCase):
    """Shared fixture: a key owner with a pin, plus an unrelated second user."""

    def setUp(self) -> None:
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)

        # Distinct names: Pin.slug uniqueness is scoped per-profile, so two
        # unnamed pins owned by different profiles can legitimately collide
        # on the same default slug - which would make this fixture's "another
        # profile's pin" tests accidentally resolve back to the caller's own.
        self.pin = baker.make(Pin, profile=self.profile, name="Steel Mill", name_is_user_provided=True)
        self.other_pin = baker.make(Pin, profile=self.other_profile, name="Rusty Foundry", name_is_user_provided=True)

        self.raw_key = self._key_with_scopes([ApiKeyScope.PANELS_READ.value])

    def _key_with_scopes(self, scopes: list[str], user: User | None = None) -> str:
        """Issue a key carrying exactly *scopes* and return its raw value."""
        api_key, raw = generate_api_key(user or self.user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=scopes)
        return raw

    def _list_url(self, pin: Pin | None = None) -> str:
        return reverse("external_api:pins.panels", kwargs={"pin_slug": (pin or self.pin).slug})

    def _detail_url(self, key: str, pin: Pin | None = None) -> str:
        return reverse("external_api:pins.panels.detail", kwargs={"pin_slug": (pin or self.pin).slug, "panel_key": key})


class RealRegistryTests(_PanelsApiTestCase):
    """Guards against the imagery carousels ever leaking onto this API."""

    def test_satellite_and_street_view_never_appear_in_the_list(self) -> None:
        """The two slide carousels stay off the API - see the module docstring."""
        response = self.client.get(self._list_url(), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        keys = {entry["key"] for entry in response.json()}
        self.assertNotIn("satellite", keys)
        self.assertNotIn("street_view", keys)

    def test_satellite_detail_is_404(self) -> None:
        """A direct request for the hidden panel is refused as though it doesn't exist."""
        response = self.client.get(self._detail_url("satellite"), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)

    def test_unknown_panel_key_is_404(self) -> None:
        """A key no source has ever claimed is a plain 404."""
        response = self.client.get(self._detail_url("not_a_real_panel"), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)

    def test_satellite_refusal_is_indistinguishable_from_unknown_key(self) -> None:
        """Same status and body either way - no oracle for "this exists but is hidden"."""
        hidden = self.client.get(self._detail_url("satellite"), **_bearer(self.raw_key))
        unknown = self.client.get(self._detail_url("not_a_real_panel"), **_bearer(self.raw_key))
        self.assertEqual(hidden.status_code, unknown.status_code)
        self.assertEqual(hidden.content, unknown.content)


class ScopeAndOwnershipTests(_PanelsApiTestCase):
    """panels:read gates both endpoints; another profile's pin is always 404."""

    def test_missing_scope_is_refused_on_list_and_detail(self) -> None:
        raw_key = self._key_with_scopes([ApiKeyScope.PINS_READ.value])
        self.assertEqual(self.client.get(self._list_url(), **_bearer(raw_key)).status_code, 403)
        self.assertEqual(self.client.get(self._detail_url("satellite"), **_bearer(raw_key)).status_code, 403)

    def test_another_profiles_pin_is_404_on_list_and_detail(self) -> None:
        self.assertEqual(self.client.get(self._list_url(self.other_pin), **_bearer(self.raw_key)).status_code, 404)
        self.assertEqual(self.client.get(self._detail_url("satellite", self.other_pin), **_bearer(self.raw_key)).status_code, 404)


class StubbedSourceListTests(_PanelsApiTestCase):
    """Full control over readiness/gate/feature via a patched registry."""

    def test_gate_false_excludes_the_source(self) -> None:
        stub = _StubPanelSource(ready=True, gated=False)
        with mock.patch.object(views_panels, "panel_sources", return_value={stub.key: stub}):
            response = self.client.get(self._list_url(), **_bearer(self.raw_key))
        self.assertEqual(response.json(), [])

    def test_ready_source_is_listed_as_ready(self) -> None:
        stub = _StubPanelSource(ready=True, gated=True)
        with mock.patch.object(views_panels, "panel_sources", return_value={stub.key: stub}):
            response = self.client.get(self._list_url(), **_bearer(self.raw_key))
        self.assertEqual(response.json(), [{"key": "stub_panel", "kinds": ["info"], "ready": True}])

    def test_not_ready_source_is_listed_as_not_ready(self) -> None:
        stub = _StubPanelSource(ready=False, gated=True)
        with mock.patch.object(views_panels, "panel_sources", return_value={stub.key: stub}):
            response = self.client.get(self._list_url(), **_bearer(self.raw_key))
        self.assertEqual(response.json(), [{"key": "stub_panel", "kinds": ["info"], "ready": False}])

    def test_feature_gated_source_omitted_without_the_grant(self) -> None:
        stub = _FeatureGatedStubPanelSource(ready=True, gated=True)
        with mock.patch.object(views_panels, "panel_sources", return_value={stub.key: stub}):
            response = self.client.get(self._list_url(), **_bearer(self.raw_key))
        self.assertEqual(response.json(), [])

    def test_feature_gated_source_listed_once_granted(self) -> None:
        stub = _FeatureGatedStubPanelSource(ready=True, gated=True)
        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)
        with mock.patch.object(views_panels, "panel_sources", return_value={stub.key: stub}):
            response = self.client.get(self._list_url(), **_bearer(self.raw_key))
        self.assertEqual([entry["key"] for entry in response.json()], ["stub_panel"])


class StubbedSourceDetailTests(_PanelsApiTestCase):
    """Ready/pending/gated permutations for the single-panel detail endpoint."""

    def test_ready_with_payload_returns_200_with_the_payload(self) -> None:
        stub = _StubPanelSource(ready=True, payload={"info": {"summary": "hi"}})
        with mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get):
            response = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"info": {"summary": "hi"}})

    def test_ready_with_no_payload_returns_204(self) -> None:
        stub = _StubPanelSource(ready=True, payload=None)
        with mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get):
            response = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 204)

    def test_not_ready_schedules_a_fetch_and_returns_202(self) -> None:
        stub = _StubPanelSource(ready=False)
        with (
            mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get),
            mock.patch.object(views_panels, "schedule_panel_fetch", return_value=True) as scheduled,
        ):
            response = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
        scheduled.assert_called_once_with(stub.key, mock.ANY)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"ready": False, "poll_after_seconds": POLL_INTERVAL_SECONDS})

    def test_not_ready_and_scheduling_fails_returns_a_longer_poll_hint(self) -> None:
        stub = _StubPanelSource(ready=False)
        with (
            mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get),
            mock.patch.object(views_panels, "schedule_panel_fetch", return_value=False),
        ):
            response = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 202)
        self.assertGreater(response.json()["poll_after_seconds"], POLL_INTERVAL_SECONDS)

    def test_gate_false_is_404(self) -> None:
        stub = _StubPanelSource(ready=True, gated=False)
        with mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get):
            response = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)

    def test_feature_gated_without_grant_is_404_indistinguishable_from_unknown(self) -> None:
        stub = _FeatureGatedStubPanelSource(ready=True, payload={"info": {}})
        with mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get):
            gated = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
            unknown = self.client.get(self._detail_url("not_a_real_panel"), **_bearer(self.raw_key))
        self.assertEqual(gated.status_code, unknown.status_code)
        self.assertEqual(gated.content, unknown.content)

    def test_feature_gated_with_grant_returns_the_payload(self) -> None:
        stub = _FeatureGatedStubPanelSource(ready=True, payload={"info": {"summary": "hi"}})
        role = baker.make(SubscriptionRole, features=SiteFeature.NEARBY_RESEARCH)
        grant_subscription(self.user, role, self.user, None)
        with mock.patch.object(views_panels, "get_panel_source", side_effect={stub.key: stub}.get):
            response = self.client.get(self._detail_url(stub.key), **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"info": {"summary": "hi"}})
