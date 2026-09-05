"""Tests for the production-only guard on REData's ML-training write surfaces.

Dev and staging deployments point at *production* REData on purpose: nearly
every REData endpoint - including the POST-shaped ones - only asks REData to go
fetch and cache third-party data about a place, so sharing one instance saves
third-party quota instead of burning it. Four endpoints are different in kind:
they send UrbanLens's own content for REData to store and train models on.
Those must never fire from a throwaway deployment, or demo data lands in the
production ML corpus indistinguishable from real data.

Two things are proved here: that the environment classifier fails closed (only
an explicit ``production`` counts, so an unset or garbage ``UL_ENVIRONMENT``
never enables writes), and that the classification actually reaches every one
of the four write surfaces while leaving reads and cache-fill calls alone.

Every HTTP call is mocked - nothing here touches the network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

from django.test import override_settings
from model_bakery import baker

from hypothesis import given, settings as hyp_settings, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.services.apis.labels.redata_labels_gateway import RedataLabelsGateway
from urbanlens.dashboard.services.apis.locations.redata_routing_gateway import RedataRoutingGateway
from urbanlens.dashboard.services.apis.photos.redata_photos_gateway import RedataPhotosGateway
from urbanlens.dashboard.services.core import environment
from urbanlens.dashboard.services.labels import redata_suggestions
from urbanlens.UrbanLens.settings._env import PRODUCTION_ENVIRONMENT_NAMES, is_production_environment
from urbanlens.UrbanLens.settings.app import settings as app_settings

_hyp = hyp_settings(max_examples=50, deadline=None)

#: Every environment name a deployment can realistically be running under that
#: is *not* production, plus the two "misconfigured" cases the guard has to
#: treat the same way: unset, and a name nobody recognises.
NON_PRODUCTION_NAMES = [
    "development",
    "local",
    "staging",
    "testing",
    None,
    "",
    "prod",
    "produktion",
    "PRODUCTION_CLONE",
    "not-production",
]


def _response(status_code: int, body: object) -> mock.Mock:
    resp = mock.Mock(status_code=status_code)
    resp.json.return_value = body
    resp.text = ""
    return resp


def _photos(session: mock.Mock) -> RedataPhotosGateway:
    return RedataPhotosGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


def _labels(session: mock.Mock) -> RedataLabelsGateway:
    return RedataLabelsGateway(base_url="https://redata.example.test", api_key="test-key", session=session)


def _as_environment(name: str | None):
    """Run a block as if ``UL_ENVIRONMENT`` were ``name``.

    Drives ``IS_PRODUCTION`` through the real classifier rather than restating
    the rule, so a test naming an environment proves the whole chain from the
    env var to the guard.
    """
    return override_settings(ENVIRONMENT_NAME=name, IS_PRODUCTION=is_production_environment(name))


class ProductionEnvironmentClassifierTests(SimpleTestCase):
    """``settings._env.is_production_environment`` recognises production and nothing else."""

    def test_production_is_production(self) -> None:
        self.assertTrue(is_production_environment("production"))

    def test_classification_ignores_case_and_surrounding_whitespace(self) -> None:
        # UL_ENVIRONMENT is read raw from a .env file, where a trailing space
        # or a capitalised value is an easy thing to end up with.
        for spelling in ("Production", "PRODUCTION", " production ", "\tproduction\n"):
            with self.subTest(spelling=spelling):
                self.assertTrue(is_production_environment(spelling))

    def test_every_known_non_production_name_is_not_production(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name):
                self.assertFalse(is_production_environment(name))

    @given(st.text(max_size=40))
    @_hyp
    def test_only_the_allow_list_is_ever_production(self, name: str) -> None:
        self.assertEqual(is_production_environment(name), name.strip().lower() in PRODUCTION_ENVIRONMENT_NAMES)


class SkipHelperTests(SimpleTestCase):
    """``services.core.environment`` answers, and reports, correctly."""

    @_as_environment("production")
    def test_production_does_not_skip(self) -> None:
        self.assertTrue(environment.is_production())
        self.assertFalse(environment.skip_upstream_contribution("a write"))

    @_as_environment("development")
    def test_non_production_skips(self) -> None:
        self.assertFalse(environment.is_production())
        self.assertTrue(environment.skip_upstream_contribution("a write"))

    def test_absent_setting_fails_closed(self) -> None:
        # A settings module that somehow never defined IS_PRODUCTION must read
        # as "not production", not raise and not default to true.
        with mock.patch.object(environment, "settings", SimpleNamespace()):
            self.assertFalse(environment.is_production())
            self.assertTrue(environment.skip_upstream_contribution("a write"))

    @_as_environment("development")
    def test_skip_is_logged_at_info_with_the_surface_and_the_environment(self) -> None:
        with self.assertLogs(environment.logger, level=logging.INFO) as captured:
            environment.skip_upstream_contribution("REData photo observations (POST /photos/)", detail="3 photo(s)")
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        # A developer wondering where their data went greps for the surface;
        # the environment name is what tells them why.
        self.assertIn("REData photo observations (POST /photos/)", record.getMessage())
        self.assertIn("3 photo(s)", record.getMessage())
        self.assertIn("development", record.getMessage())

    @_as_environment("development")
    def test_skip_is_not_a_warning_or_an_error(self) -> None:
        # Running outside production is the normal case; flagging it as a
        # problem would train developers to ignore the log.
        with self.assertLogs(environment.logger, level=logging.DEBUG) as captured:
            environment.skip_upstream_contribution("a write")
        self.assertEqual([record.levelno for record in captured.records], [logging.INFO])

    @_as_environment("production")
    def test_nothing_is_logged_on_production(self) -> None:
        with self.assertNoLogs(environment.logger, level=logging.DEBUG):
            environment.skip_upstream_contribution("a write")


class TrueWriteSurfacesAreSkippedOffProductionTests(SimpleTestCase):
    """The four endpoints that store UrbanLens's own data send nothing off production."""

    def test_submit_photos_sends_nothing(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                result = _photos(session).submit_photos(
                    [{"photo_id": "abc", "location_latitude": 1.0, "location_longitude": 2.0}]
                )
                session.post.assert_not_called()
                # Indistinguishable from "submitted, nothing scored yet" - the
                # caller caches no confidence and reports no failure.
                self.assertEqual(result["count"], 0)
                self.assertEqual(result["results"], {})

    def test_submit_votes_sends_nothing_and_reports_the_photos_as_unknown(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                result = _photos(session).submit_votes(
                    [{"photo_id": "abc", "is_relevant": True}, {"photo_id": "def", "is_relevant": False}]
                )
                session.post.assert_not_called()
                self.assertEqual(result["recorded"], 0)
                # The truthful shape, and the same one production returns for a
                # photo REData was never told about - so callers need no branch.
                self.assertEqual(result["unknown_photo_ids"], ["abc", "def"])

    def test_define_labels_sends_nothing(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                result = _labels(session).define_labels("user-1", [{"external_id": "abc", "name": "Church"}])
                session.post.assert_not_called()
                self.assertEqual(result["created"], 0)
                self.assertEqual(result["updated"], 0)

    def test_sync_assignments_sends_nothing(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                result = _labels(session).sync_assignments(
                    "user-1",
                    [
                        {
                            "external_id": "pin-1",
                            "latitude": 1.0,
                            "longitude": 2.0,
                            "label_ids": ["abc"],
                            "replace": True,
                        }
                    ],
                )
                session.post.assert_not_called()
                self.assertEqual(result["locations_created"], 0)
                self.assertEqual(result["assignments_added"], 0)

    def test_a_skipped_write_raises_nothing(self) -> None:
        # A skip must look like an ordinary empty result to the Celery task that
        # queued it, not like an outage - otherwise it is logged as a failure
        # and, for any caller that grows a retry, retried forever.
        with _as_environment("staging"):
            session = mock.Mock()
            gateway = _photos(session)
            gateway.submit_photos([{"photo_id": "abc", "location_latitude": 1.0, "location_longitude": 2.0}])
            gateway.submit_votes([{"photo_id": "abc", "is_relevant": True}])


class TrueWriteSurfacesAreAttemptedOnProductionTests(SimpleTestCase):
    """The same four endpoints are sent normally when the deployment is production."""

    @_as_environment("production")
    def test_submit_photos_posts(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"count": 1, "results": {"abc": {"confidence": 0.9}}, "unknown": []})
        result = _photos(session).submit_photos(
            [{"photo_id": "abc", "location_latitude": 1.0, "location_longitude": 2.0}]
        )
        self.assertEqual(session.post.call_args.args[0], "https://redata.example.test/api/v1/photos/")
        self.assertEqual(result["results"]["abc"]["confidence"], 0.9)

    @_as_environment("production")
    def test_submit_votes_posts(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"recorded": 1, "unknown_photo_ids": [], "updated_photos": 1})
        result = _photos(session).submit_votes([{"photo_id": "abc", "is_relevant": True}])
        self.assertEqual(session.post.call_args.args[0], "https://redata.example.test/api/v1/photos/votes/")
        self.assertEqual(result["recorded"], 1)

    @_as_environment("production")
    def test_define_labels_posts(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"created": 1, "updated": 0})
        _labels(session).define_labels("user-1", [{"external_id": "abc", "name": "Church"}])
        self.assertEqual(session.post.call_args.args[0], "https://redata.example.test/api/v1/labels/")

    @_as_environment("production")
    def test_sync_assignments_posts(self) -> None:
        session = mock.Mock()
        session.post.return_value = _response(200, {"locations_created": 1})
        _labels(session).sync_assignments(
            "user-1",
            [{"external_id": "pin-1", "latitude": 1.0, "longitude": 2.0, "label_ids": ["abc"], "replace": True}],
        )
        self.assertEqual(session.post.call_args.args[0], "https://redata.example.test/api/v1/labels/assignments/")


class ReadAndCacheFillSurfacesAreUnaffectedTests(SimpleTestCase):
    """Everything that is not a contribution still calls REData from any environment.

    This is the whole point of the distinction: dev pointed at production REData
    is *better* than dev with its own instance, so the guard must not creep past
    the four surfaces that actually store our data. Three of the calls below are
    POSTs - being a POST is not what makes an endpoint a write.
    """

    def test_get_confidence_batch_is_a_post_shaped_read_and_still_calls(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                session.post.return_value = _response(
                    200, {"count": 1, "results": {"abc": {"confidence": 0.5}}, "unknown": []}
                )
                result = _photos(session).get_confidence_batch(["abc"])
                self.assertEqual(
                    session.post.call_args.args[0], "https://redata.example.test/api/v1/photos/confidence/"
                )
                self.assertEqual(result["results"]["abc"]["confidence"], 0.5)

    def test_suggest_labels_is_a_post_shaped_read_and_still_calls(self) -> None:
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                session.post.return_value = _response(200, {"count": 0, "results": [], "ranker": "heuristic"})
                _labels(session).suggest_labels("user-1", 42.0, -73.0)
                self.assertEqual(session.post.call_args.args[0], "https://redata.example.test/api/v1/labels/suggest/")

    def test_get_model_still_calls(self) -> None:
        with _as_environment("development"):
            session = mock.Mock()
            session.get.return_value = _response(200, {"active": None})
            _photos(session).get_model()
            self.assertEqual(session.get.call_args.args[0], "https://redata.example.test/api/v1/photos/model/")

    def test_routing_post_is_not_a_contribution_and_still_calls(self) -> None:
        # POST /routes/ sends waypoints so REData can compute an answer; it
        # stores nothing of ours and trains on nothing.
        for name in NON_PRODUCTION_NAMES:
            with self.subTest(name=name), _as_environment(name):
                session = mock.Mock()
                session.post.return_value = _response(
                    200, {"route": {"distance_meters": 100.0, "duration_seconds": 60.0}}
                )
                gateway = RedataRoutingGateway(
                    base_url="https://redata.example.test", api_key="test-key", session=session
                )
                self.assertEqual(
                    gateway.get_route([(41.0, -73.9), (41.1, -73.8)]),
                    {"distance_meters": 100.0, "duration_seconds": 60.0},
                )
                session.post.assert_called_once()


class BackfillProfileGuardTests(TestCase):
    """The label backfill reports what it actually sent, which off production is nothing."""

    def setUp(self) -> None:
        from django.contrib.auth.models import User

        self.profile = baker.make(User).profile
        for attribute, value in (("redata_api_url", "https://redata.example.test"), ("redata_api_key", "test-key")):
            patcher = mock.patch.object(app_settings, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_off_production_returns_zero_counts_and_builds_no_gateway(self) -> None:
        with (
            _as_environment("development"),
            mock.patch(
                "urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway"
            ) as gateway_class,
        ):
            self.assertEqual(redata_suggestions.backfill_profile(self.profile), (0, 0))
        gateway_class.assert_not_called()

    def test_on_production_syncs(self) -> None:
        with (
            _as_environment("production"),
            mock.patch(
                "urbanlens.dashboard.services.apis.labels.redata_labels_gateway.RedataLabelsGateway"
            ) as gateway_class,
        ):
            labels_synced, _pins_synced = redata_suggestions.backfill_profile(self.profile)
        gateway_class.assert_called()
        self.assertGreater(labels_synced, 0)
