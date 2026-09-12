"""Tests for the two REData panels added 2026-09-08: Incident History and Historical Features."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.subscriptions import SiteFeature, SubscriptionRole, grant_subscription
from urbanlens.dashboard.plugins.builtin.redata_historical_features import (
    HistoricalFeaturesPanelSource,
    HistoricalFeaturesPlugin,
)
from urbanlens.dashboard.plugins.builtin.redata_incidents import IncidentHistoryPanelSource
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextEnvelope
from urbanlens.dashboard.services.core import rate_limiter
from urbanlens.dashboard.services.pins.external_data import get_panel_source
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

if TYPE_CHECKING:
    from urbanlens.dashboard.models.pin.model import Pin


def _us_pin() -> Pin:
    location = baker.make("dashboard.Location", latitude=40.5, longitude=-74.5)
    return baker.make_recipe("dashboard.pin", profile=baker.make(User).profile, location=location)


class IncidentHistoryPanelRenderTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = IncidentHistoryPanelSource()
        self.pin = _us_pin()

    def test_declares_the_gate(self) -> None:
        """The whole point of this panel: it must require the new flag."""
        self.assertEqual(self.source.required_feature, SiteFeature.INCIDENT_HISTORY)

    def test_years_grouped_most_recent_first_with_undated_last(self) -> None:
        """A plain string sort would put "Undated" ahead of every real year ("U" > "2")."""
        data = {
            "incidents": [
                {"category": "theft", "occurred_at": "2024-03-01"},
                {"category": "burglary", "occurred_at": "2026-01-02"},
                {"category": "assault", "occurred_at": "2026-05-01"},
                {"category": "vandalism", "occurred_at": ""},
            ]
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = [entry["label"] for entry in ctx["meta"]]
        self.assertEqual(labels, ["2026", "2024", "Undated", "Precision"])
        self.assertEqual(next(e for e in ctx["meta"] if e["label"] == "2026")["value"], "2 incidents")

    def test_traffic_collisions_are_excluded(self) -> None:
        data = {"incidents": [{"category": "traffic", "occurred_at": "2020-01-01"}]}
        self.assertIsNone(self.source.render_context(self.pin, data))

    def test_real_iso8601_datetime_is_bucketed_by_year_not_marked_undated(self) -> None:
        """REData's occurred_at is a full ISO-8601 datetime with an offset (e.g. "2026-08-03T17:03:00-05:00", 25 chars), never a bare "YYYY-MM-DD" - a check requiring an exact 10-character value would misclassify every real incident as undated."""
        data = {"incidents": [{"category": "burglary", "occurred_at": "2026-08-03T17:03:00-05:00"}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        labels = [entry["label"] for entry in ctx["meta"]]
        self.assertIn("2026", labels)
        self.assertNotIn("Undated", labels)

    def test_empty_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"incidents": []}))

    def test_fetches_the_full_25_year_window(self) -> None:
        """The whole reason this panel exists rather than reusing the free one's fetch."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_incidents_gateway.RedataIncidentsGateway"
        ) as gateway_cls:
            gateway_cls.return_value.get_incidents.return_value = LocationContextEnvelope(
                count=0, complete=True, results=[]
            )
            self.source.fetch_envelope(40.5, -74.5)
        gateway_cls.return_value.get_incidents.assert_called_once_with(
            40.5, -74.5, years=25, limit=500, force_refresh=True
        )

    def test_forces_a_live_refresh_so_the_free_panels_cache_cannot_truncate_the_window(self) -> None:
        """If the free 3-year panel populates that cache first (the common case, since it is the default panel), an unforced fetch here would silently be served those same narrow 3-year rows for a full cache window with no error and no way to tell."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_incidents_gateway.RedataIncidentsGateway"
        ) as gateway_cls:
            gateway_cls.return_value.get_incidents.return_value = LocationContextEnvelope(
                count=0, complete=True, results=[]
            )
            self.source.fetch_envelope(40.5, -74.5)
        _, kwargs = gateway_cls.return_value.get_incidents.call_args
        self.assertTrue(kwargs.get("force_refresh"))

    def test_the_free_panel_does_not_force_refresh(self) -> None:
        """Anti-vacuity: force_refresh must stay scoped to the paid panel. Forcing it on
        the free panel too would silently regress its whole caching benefit.
        """
        from urbanlens.dashboard.plugins.builtin.redata_incidents import PoliceIncidentsPanelSource

        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_incidents_gateway.RedataIncidentsGateway"
        ) as gateway_cls:
            gateway_cls.return_value.get_incidents.return_value = LocationContextEnvelope(
                count=0, complete=True, results=[]
            )
            PoliceIncidentsPanelSource().fetch_envelope(40.5, -74.5)
        _, kwargs = gateway_cls.return_value.get_incidents.call_args
        self.assertFalse(kwargs.get("force_refresh"))

    def test_cache_source_is_independent_of_the_free_panel(self) -> None:
        """Different years/limit fetches must never collide in LocationCache."""
        from urbanlens.dashboard.plugins.builtin.redata_incidents import PoliceIncidentsPanelSource

        self.assertNotEqual(self.source.cache_source, PoliceIncidentsPanelSource().cache_source)


class IncidentHistoryPanelGateTests(RedataConfiguredMixin, TestCase):
    """End-to-end: the concrete gate on the real new source, not just the mocked mechanism."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs bootstrap site-admin promotion
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=self.user.profile,
            location=baker.make("dashboard.Location", latitude=40.5, longitude=-74.5),
        )
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        LocationCache.set(
            self.pin.location,
            "redata_incident_history",
            {
                "incidents": [
                    {"category": "burglary", "occurred_at": "2020-01-01", "offense_description": "forced entry"}
                ]
            },
            query_key="40.50000,-74.50000",
        )

    def test_viewer_without_the_feature_is_refused(self) -> None:
        from django.urls import reverse

        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "redata_incident_history"]))
        self.assertEqual(response.status_code, 404)

    def test_viewer_with_the_feature_sees_it(self) -> None:
        from django.urls import reverse

        role = baker.make(SubscriptionRole, features=SiteFeature.INCIDENT_HISTORY)
        grant_subscription(self.user, role, self.user, None)
        response = self.client.get(reverse("pin.panel", args=[self.pin.slug, "redata_incident_history"]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Burglary")

    def test_the_free_sibling_panel_is_unaffected(self) -> None:
        """Gating the history panel must not touch the always-free recent-incidents card."""
        from urbanlens.dashboard.plugins.builtin.redata_incidents import PoliceIncidentsPanelSource

        self.assertIsNone(PoliceIncidentsPanelSource.required_feature)


class HistoricalFeaturesPanelTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = HistoricalFeaturesPanelSource()
        self.pin = _us_pin()

    def test_empty_hides_the_panel(self) -> None:
        self.assertIsNone(self.source.render_context(self.pin, {"features": []}))

    def test_dated_range_and_source_note_render(self) -> None:
        data = {
            "features": [
                {
                    "kind": "building",
                    "name": "Odd Fellows Hall",
                    "start_year": 1898,
                    "end_year": 1962,
                    "source_note": "1909 Sanborn Fire Insurance map",
                },
            ]
        }
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["label"], "Odd Fellows Hall")
        self.assertIn("1898–1962", ctx["meta"][0]["value"])
        self.assertIn("Sanborn", ctx["meta"][0]["value"])

    def test_null_end_year_is_not_presented_as_still_standing(self) -> None:
        """A null end_year means "not known to have ended", never "confirmed standing" - see the gateway docstring."""
        data = {"features": [{"kind": "road", "name": "Old Mill Road", "start_year": 1850, "end_year": None}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertNotIn("present", ctx["meta"][0]["value"].lower())
        self.assertIn("documented from 1850", ctx["meta"][0]["value"])
        self.assertIn("1 with no recorded end date", ctx["chips"])

    def test_a_start_year_of_zero_is_not_dropped_as_falsy(self) -> None:
        """0 is a valid (if unlikely) year, not an absent bound - a truthiness check would drop it."""
        data = {"features": [{"kind": "building", "name": "Ancient Foundation", "start_year": 0, "end_year": None}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertIn("documented from 0", ctx["meta"][0]["value"])

    def test_unnamed_feature_falls_back_to_its_kind_label(self) -> None:
        data = {"features": [{"kind": "water", "name": "", "start_year": None, "end_year": None}]}
        ctx = self.source.render_context(self.pin, data)
        assert ctx is not None
        self.assertEqual(ctx["meta"][0]["label"], "Water feature")

    def test_fetch_caches_results_without_geometry(self) -> None:
        from urbanlens.dashboard.models.cache.location_cache import LocationCache

        envelope = LocationContextEnvelope(
            count=1,
            complete=True,
            results=[
                {"kind": "building", "name": "Odd Fellows Hall", "geometry": {"type": "Point", "coordinates": [0, 0]}}
            ],
        )
        with mock.patch(
            "urbanlens.dashboard.services.apis.locations.redata_historical_features_gateway.RedataHistoricalFeaturesGateway"
        ) as gateway_cls:
            gateway_cls.return_value.get_historical_features.return_value = envelope
            self.source.fetch(self.pin)

        cached = LocationCache.get_fresh(self.pin.location, "redata_historical_features")
        assert cached is not None
        self.assertEqual(cached.data["features"][0]["name"], "Odd Fellows Hall")
        self.assertNotIn("geometry", cached.data["features"][0])

    def test_is_free_not_subscriber_gated(self) -> None:
        """Unlike Incident History, this new domain has no cost profile that warrants gating it."""
        self.assertIsNone(self.source.required_feature)


class NewPanelsAreRegisteredTests(TestCase):
    """Both plugins auto-discover via ``pkgutil.iter_modules`` - confirm they actually land in the registry."""

    def test_incident_history_is_registered(self) -> None:
        self.assertIsNotNone(get_panel_source("redata_incident_history"))

    def test_historical_features_is_registered(self) -> None:
        self.assertIsNotNone(get_panel_source("redata_historical_features"))


class HistoricalFeaturesRateLimitTests(TestCase):
    """A gateway with no registered defaults is unbudgeted, not free - see redata_historic_registers's own test."""

    def test_the_plugin_declares_its_own_service_key(self) -> None:
        self.assertIn("redata_historical_features", HistoricalFeaturesPlugin().get_service_defaults())

    def test_get_limit_config_uses_the_declared_defaults_not_the_generic_fallback(self) -> None:
        """Without a get_service_defaults() override, get_limit_config() falls through to the generic 20/min-500/day default with no notes - indistinguishable from a service nobody ever configured. calls_per_day=None here is what tells the two cases apart."""
        config = rate_limiter.get_limit_config("redata_historical_features")

        self.assertEqual(config.display_name, "REData Historical Features")
        self.assertEqual(config.calls_per_minute, 20)
        self.assertIsNone(config.calls_per_day)
        self.assertNotEqual(config.notes, "")
