"""A provider that stops answering is noticed, backed off, probed and reported, without a person going looking.

On 2026-10-05 the dev deployment's Cloudflare image classifier had failed 33 of its last 40 calls and REData was
answering 503 to a quarter of the calls made to it, and nothing had said so.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.provider_health import BackoffCause, ProviderHealth, ProviderState
from urbanlens.dashboard.services.core import provider_health
from urbanlens.dashboard.services.core.background_work import background_work, is_background, queue_is_background
from urbanlens.dashboard.services.core.outages import outages_observed
from urbanlens.dashboard.services.core.provider_health import (
    Baseline,
    Outcome,
    Tally,
    backoff_duration,
    evaluate_provider_health,
    judge,
)
from urbanlens.dashboard.services.core.rate_limiter import UpstreamThrottledError, _RateLimitedSession, api_call_slot
from urbanlens.dashboard.services.sandbox.queues import Queue

_SERVICE = "google_geocoding"
_URL = "https://maps.example.test/geocode"


def _calls(
    service: str, count: int, *, status: int | None = 200, at: datetime, success: bool | None = None, **flags: object
) -> None:
    """``count`` logged calls at ``at``, as the rate limiter would have finalized them."""
    ok = (status is not None and status < 400) if success is None else success
    rows = ApiCallLog.objects.bulk_create(
        [ApiCallLog(service=service, success=ok, status_code=status, response_ms=40, **flags) for _ in range(count)]
    )
    ApiCallLog.objects.filter(pk__in=[row.pk for row in rows]).update(created=at)


def _backed_off(service: str = _SERVICE, *, state: str = ProviderState.BACKED_OFF, minutes: int = 30) -> ProviderHealth:
    row = ProviderHealth.objects.create(
        provider=service,
        state=state,
        cause=BackoffCause.FAILING,
        backed_off_until=timezone.now() + timedelta(minutes=minutes) if state == ProviderState.BACKED_OFF else None,
        episode_started_at=timezone.now(),
    )
    provider_health.write_snapshot()
    return row


def _response(status: int) -> mock.Mock:
    return mock.Mock(status_code=status, headers={}, ok=200 <= status < 400, text="{}")


def _session(service: str = _SERVICE, status: int = 200) -> _RateLimitedSession:
    session = _RateLimitedSession(service)
    session._session = mock.Mock()
    session._session.request.return_value = _response(status)
    return session


class _GateTestCase(TestCase):
    """Leaves no backoff behind: a later test outside the cache-clearing base classes must not inherit it."""

    def tearDown(self) -> None:
        provider_health.cache.delete(provider_health._SNAPSHOT_KEY)
        provider_health.forget_snapshot()
        super().tearDown()


class JudgeTests(SimpleTestCase):
    def test_too_few_calls_say_nothing(self) -> None:
        self.assertIsNone(judge(Tally(failed=9)))

    def test_mostly_refused_is_backed_off_for_refusing(self) -> None:
        verdict = judge(Tally(ok=4, refused=6))
        assert verdict is not None
        self.assertEqual((verdict.state, verdict.cause), (ProviderState.BACKED_OFF, BackoffCause.REFUSED))

    def test_hardly_answering_is_backed_off_as_failing(self) -> None:
        verdict = judge(Tally(ok=7, failed=33))
        assert verdict is not None
        self.assertEqual((verdict.state, verdict.cause), (ProviderState.BACKED_OFF, BackoffCause.FAILING))

    def test_a_few_failures_are_healthy(self) -> None:
        verdict = judge(Tally(ok=30, failed=10))
        assert verdict is not None
        self.assertEqual(verdict.state, ProviderState.HEALTHY)

    def test_answering_half_as_often_as_normal_is_degraded(self) -> None:
        verdict = judge(Tally(ok=12, failed=28), Baseline(attempts=500, answered_share=0.95, empty_share=0.05))
        assert verdict is not None
        self.assertEqual((verdict.state, verdict.cause), (ProviderState.DEGRADED, BackoffCause.BELOW_BASELINE))

    def test_answering_empty_far_more_than_normal_is_degraded(self) -> None:
        verdict = judge(Tally(ok=10, empty=30), Baseline(attempts=500, answered_share=0.95, empty_share=0.1))
        assert verdict is not None
        self.assertEqual(verdict.state, ProviderState.DEGRADED)

    def test_a_baseline_from_too_few_calls_is_ignored(self) -> None:
        verdict = judge(Tally(ok=12, failed=28), Baseline(attempts=20, answered_share=0.95, empty_share=0.05))
        assert verdict is not None
        self.assertEqual(verdict.state, ProviderState.HEALTHY)

    def test_backoffs_double_from_fifteen_minutes_up_to_a_day(self) -> None:
        self.assertEqual(
            [backoff_duration(level) for level in (1, 2, 3, 7, 30)],
            [
                timedelta(minutes=15),
                timedelta(minutes=30),
                timedelta(hours=1),
                timedelta(hours=16),
                timedelta(hours=24),
            ],
        )


class OutcomeTests(_GateTestCase):
    def test_each_kind_of_logged_call_has_its_outcome(self) -> None:
        at = timezone.now() - timedelta(minutes=5)
        _calls(_SERVICE, 1, status=200, at=at)
        _calls(_SERVICE, 1, status=404, at=at)
        _calls(_SERVICE, 1, status=429, at=at)
        _calls(_SERVICE, 1, status=403, at=at)
        _calls(_SERVICE, 1, status=503, at=at)
        _calls(_SERVICE, 1, status=None, success=False, at=at)
        _calls(_SERVICE, 1, status=None, success=True, at=at)

        counts = provider_health._read_counts(timezone.now() - timedelta(hours=1))

        self.assertEqual(
            sorted((count.status_code or 0, count.outcome) for count in counts),
            [
                (0, Outcome.FAILED),
                (0, Outcome.OK),
                (200, Outcome.OK),
                (403, Outcome.REFUSED),
                (404, Outcome.EMPTY),
                (429, Outcome.REFUSED),
                (503, Outcome.FAILED),
            ],
        )

    def test_calls_never_sent_and_calls_still_in_flight_are_left_out(self) -> None:
        at = timezone.now() - timedelta(minutes=5)
        _calls(_SERVICE, 3, status=None, success=False, at=at, was_rate_limited=True)
        _calls(_SERVICE, 3, status=None, success=False, at=at, was_service_disabled=True)
        _calls(_SERVICE, 3, status=None, success=False, at=at, was_geo_filtered=True)
        rows = ApiCallLog.objects.bulk_create([ApiCallLog(service=_SERVICE, success=True) for _ in range(3)])
        ApiCallLog.objects.filter(pk__in=[row.pk for row in rows]).update(created=at)

        self.assertEqual(provider_health._read_counts(timezone.now() - timedelta(hours=1)), [])


class EvaluationTests(_GateTestCase):
    def test_a_provider_failing_most_calls_is_backed_off_for_fifteen_minutes(self) -> None:
        now = timezone.now()
        _calls(_SERVICE, 7, status=200, at=now - timedelta(minutes=5))
        _calls(_SERVICE, 33, status=None, success=False, at=now - timedelta(minutes=5))

        report = evaluate_provider_health(now=now)

        row = ProviderHealth.objects.get(provider=_SERVICE)
        self.assertEqual((row.state, row.cause, row.level), (ProviderState.BACKED_OFF, BackoffCause.FAILING, 1))
        self.assertEqual(row.backed_off_until, now + timedelta(minutes=15))
        self.assertEqual((row.attempts, row.answered, row.failed), (40, 7, 33))
        self.assertIn(f"{_SERVICE}: backed off for 15 min", report.transitions)

    def test_a_healthy_provider_stays_healthy(self) -> None:
        now = timezone.now()
        _calls(_SERVICE, 40, status=200, at=now - timedelta(minutes=5))

        evaluate_provider_health(now=now)

        self.assertEqual(ProviderHealth.objects.get(provider=_SERVICE).state, ProviderState.HEALTHY)

    def test_a_rarely_called_provider_is_judged_on_the_day(self) -> None:
        now = timezone.now()
        _calls(_SERVICE, 12, status=429, at=now - timedelta(hours=6))

        evaluate_provider_health(now=now)

        row = ProviderHealth.objects.get(provider=_SERVICE)
        self.assertEqual(
            (row.state, row.cause, row.window_minutes), (ProviderState.BACKED_OFF, BackoffCause.REFUSED, 1440)
        )

    def test_a_finished_backoff_is_probed_and_passing_probes_recover_it(self) -> None:
        start = timezone.now() - timedelta(hours=2)
        _calls(_SERVICE, 20, status=503, at=start - timedelta(minutes=5))
        evaluate_provider_health(now=start)

        evaluate_provider_health(now=start + timedelta(minutes=16))
        row = ProviderHealth.objects.get(provider=_SERVICE)
        self.assertEqual(row.state, ProviderState.PROBING)

        _calls(_SERVICE, 3, status=200, at=row.state_since + timedelta(minutes=11))
        evaluate_provider_health(now=row.state_since + timedelta(minutes=25))

        row.refresh_from_db()
        self.assertEqual((row.state, row.backed_off_until, row.level), (ProviderState.HEALTHY, None, 1))
        self.assertIsNotNone(row.counted_from)

    def test_failing_probes_back_it_off_for_twice_as_long(self) -> None:
        start = timezone.now() - timedelta(hours=2)
        _calls(_SERVICE, 20, status=503, at=start - timedelta(minutes=5))
        evaluate_provider_health(now=start)
        evaluate_provider_health(now=start + timedelta(minutes=16))
        row = ProviderHealth.objects.get(provider=_SERVICE)

        _calls(_SERVICE, 3, status=503, at=row.state_since + timedelta(minutes=11))
        later = row.state_since + timedelta(minutes=25)
        evaluate_provider_health(now=later)

        row.refresh_from_db()
        self.assertEqual((row.state, row.level), (ProviderState.BACKED_OFF, 2))
        self.assertEqual(row.backed_off_until, later + timedelta(minutes=30))

    def test_the_failures_that_backed_a_provider_off_do_not_back_it_off_again(self) -> None:
        start = timezone.now() - timedelta(hours=2)
        _calls(_SERVICE, 20, status=503, at=start - timedelta(minutes=5))
        evaluate_provider_health(now=start)
        evaluate_provider_health(now=start + timedelta(minutes=16))
        row = ProviderHealth.objects.get(provider=_SERVICE)
        _calls(_SERVICE, 3, status=200, at=row.state_since + timedelta(minutes=11))
        evaluate_provider_health(now=row.state_since + timedelta(minutes=25))

        evaluate_provider_health(now=row.state_since + timedelta(minutes=30))

        row.refresh_from_db()
        self.assertEqual(row.state, ProviderState.HEALTHY)

    def test_probes_in_the_minutes_after_probing_began_count(self) -> None:
        now = timezone.now()
        ProviderHealth.objects.create(
            provider=_SERVICE,
            state=ProviderState.PROBING,
            cause=BackoffCause.FAILING,
            level=1,
            state_since=now - timedelta(minutes=5),
        )
        _calls(_SERVICE, 3, status=200, at=now - timedelta(minutes=3))

        evaluate_provider_health(now=now)

        self.assertEqual(ProviderHealth.objects.get(provider=_SERVICE).state, ProviderState.HEALTHY)

    def test_a_probing_provider_nothing_calls_for_a_day_is_cleared(self) -> None:
        now = timezone.now()
        ProviderHealth.objects.create(
            provider=_SERVICE,
            state=ProviderState.PROBING,
            cause=BackoffCause.FAILING,
            level=3,
            state_since=now - timedelta(hours=25),
            episode_started_at=now - timedelta(days=2),
        )

        evaluate_provider_health(now=now)

        row = ProviderHealth.objects.get(provider=_SERVICE)
        self.assertEqual((row.state, row.level), (ProviderState.HEALTHY, 3))
        self.assertIn("Not called enough to probe", row.reason)

    def test_a_degraded_provider_nothing_calls_any_more_is_cleared(self) -> None:
        now = timezone.now()
        ProviderHealth.objects.create(
            provider=_SERVICE,
            state=ProviderState.DEGRADED,
            cause=BackoffCause.BELOW_BASELINE,
            state_since=now - timedelta(days=9),
            episode_started_at=now - timedelta(days=9),
        )

        evaluate_provider_health(now=now)

        self.assertEqual(ProviderHealth.objects.get(provider=_SERVICE).state, ProviderState.HEALTHY)

    def test_the_evaluation_publishes_the_backoff_to_the_gate(self) -> None:
        now = timezone.now()
        _calls(_SERVICE, 20, status=503, at=now - timedelta(minutes=5))

        evaluate_provider_health(now=now)

        refusal = provider_health.refusal_for(_SERVICE, background=True)
        assert refusal is not None
        self.assertGreater(refusal.retry_after, 14 * 60)


class AlertTests(_GateTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(provider_health, "alerts_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        notify = mock.patch("urbanlens.dashboard.services.notifications.notifications.notify")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def _fail(self, at: datetime) -> None:
        _calls(_SERVICE, 20, status=503, at=at - timedelta(minutes=5))
        evaluate_provider_health(now=at)

    def test_a_blip_shorter_than_half_an_hour_tells_nobody(self) -> None:
        start = timezone.now() - timedelta(hours=1)
        self._fail(start)
        evaluate_provider_health(now=start + timedelta(minutes=10))

        self.notify.assert_not_called()

    def test_half_an_hour_unhealthy_sends_one_digest_on_the_provider_health_event(self) -> None:
        start = timezone.now() - timedelta(hours=2)
        self._fail(start)

        evaluate_provider_health(now=start + timedelta(minutes=35))
        evaluate_provider_health(now=start + timedelta(minutes=40))

        self.notify.assert_called_once()
        event, subject, message = self.notify.call_args.args
        self.assertEqual(event, "provider_health")
        self.assertIn("1 provider refusing or failing", subject)
        self.assertIn(_SERVICE, message)

    def test_a_long_episode_is_reported_again_each_day(self) -> None:
        start = timezone.now() - timedelta(days=3)
        self._fail(start)
        evaluate_provider_health(now=start + timedelta(minutes=35))

        evaluate_provider_health(now=start + timedelta(hours=24, minutes=40))

        self.assertEqual(self.notify.call_count, 2)

    def test_a_reported_provider_that_recovers_is_reported_recovered(self) -> None:
        start = timezone.now() - timedelta(hours=3)
        self._fail(start)
        evaluate_provider_health(now=start + timedelta(minutes=35))
        row = ProviderHealth.objects.get(provider=_SERVICE)
        self.assertEqual(row.state, ProviderState.PROBING)
        _calls(_SERVICE, 3, status=200, at=row.state_since + timedelta(minutes=11))

        evaluate_provider_health(now=row.state_since + timedelta(minutes=25))

        self.assertEqual(self.notify.call_count, 2)
        self.assertIn("recovered", self.notify.call_args.args[1])

    def test_a_degraded_provider_that_then_backs_off_is_reported_again(self) -> None:
        now = timezone.now()
        ProviderHealth.objects.create(
            provider=_SERVICE,
            state=ProviderState.DEGRADED,
            cause=BackoffCause.BELOW_BASELINE,
            state_since=now - timedelta(hours=2),
            episode_started_at=now - timedelta(hours=2),
            alerted_at=now - timedelta(hours=1),
        )
        _calls(_SERVICE, 20, status=503, at=now - timedelta(minutes=5))

        evaluate_provider_health(now=now)

        self.assertEqual(ProviderHealth.objects.get(provider=_SERVICE).state, ProviderState.BACKED_OFF)
        self.notify.assert_called_once()

    def test_a_few_lone_failed_calls_are_not_a_network_outage(self) -> None:
        now = timezone.now()
        for service in ("nominatim", "overpass", "wikidata"):
            _calls(service, 1, status=500, at=now - timedelta(minutes=5))
        for service in ("open_meteo", "usgs_earthquakes"):
            _calls(service, 3, status=200, at=now - timedelta(minutes=5))
        ProviderHealth.objects.create(
            provider=_SERVICE,
            state=ProviderState.BACKED_OFF,
            cause=BackoffCause.FAILING,
            backed_off_until=now + timedelta(hours=5),
            episode_started_at=now - timedelta(days=2),
            alerted_at=now - timedelta(hours=25),
        )

        evaluate_provider_health(now=now)

        self.assertNotIn("network may be down", self.notify.call_args.args[1])

    def test_a_cleared_provider_is_reported_as_too_rarely_called(self) -> None:
        now = timezone.now()
        ProviderHealth.objects.create(
            provider=_SERVICE,
            state=ProviderState.PROBING,
            level=2,
            state_since=now - timedelta(hours=25),
            episode_started_at=now - timedelta(days=2),
            alerted_at=now - timedelta(hours=20),
        )

        evaluate_provider_health(now=now)

        self.assertIn(
            f"{_SERVICE} cleared after 2 days: called too rarely to judge any more.", self.notify.call_args.args[2]
        )

    def test_redata_down_is_named_rather_than_counted(self) -> None:
        start = timezone.now() - timedelta(hours=2)
        for service in ("redata_places", "redata_api", "redata_cid_lookup"):
            _calls(service, 20, status=503, at=start - timedelta(minutes=5))
        evaluate_provider_health(now=start)

        evaluate_provider_health(now=start + timedelta(minutes=35))

        self.assertIn("REData is failing", self.notify.call_args.args[1])

    def test_a_failed_delivery_still_leaves_the_backoff_in_force(self) -> None:
        self.notify.side_effect = RuntimeError("smtp down")
        start = timezone.now() - timedelta(hours=1)
        self._fail(start)

        evaluate_provider_health(now=start + timedelta(minutes=35))

        provider_health.forget_snapshot()
        self.assertEqual(provider_health._snapshot()[_SERVICE].state, ProviderState.PROBING)
        self.assertIsNone(ProviderHealth.objects.get(provider=_SERVICE).alerted_at)


class GateTests(_GateTestCase):
    def test_a_backed_off_provider_refuses_background_work_without_a_request(self) -> None:
        _backed_off()
        session = _session()

        with background_work(), outages_observed() as outages, pytest.raises(UpstreamThrottledError) as caught:
            session.get(_URL)

        session._session.request.assert_not_called()
        self.assertGreater(caught.value.retry_after, 29 * 60)
        self.assertEqual(outages.services, [_SERVICE])
        row = ApiCallLog.objects.get(service=_SERVICE)
        self.assertTrue(row.was_rate_limited)

    def test_live_work_keeps_a_trickle(self) -> None:
        _backed_off()
        session = _session()

        for _ in range(provider_health.LIVE_TRICKLE_CALLS):
            session.get(_URL)
        with pytest.raises(UpstreamThrottledError):
            session.get(_URL)

        self.assertEqual(session._session.request.call_count, provider_health.LIVE_TRICKLE_CALLS)

    def test_a_probing_provider_gets_one_background_call_per_interval(self) -> None:
        _backed_off(state=ProviderState.PROBING)
        session = _session()

        with background_work():
            session.get(_URL)
            with pytest.raises(UpstreamThrottledError):
                session.get(_URL)

        self.assertEqual(session._session.request.call_count, 1)

    def test_other_providers_are_untouched(self) -> None:
        _backed_off()

        with background_work():
            _session("redata_places").get(_URL)

    def test_sdk_calls_are_refused_before_their_block_runs(self) -> None:
        _backed_off()
        ran = mock.Mock()

        with background_work(), pytest.raises(UpstreamThrottledError), api_call_slot(_SERVICE):
            ran()

        ran.assert_not_called()
        self.assertEqual(ApiCallLog.objects.filter(service=_SERVICE, was_rate_limited=False).count(), 0)

    def test_an_unreadable_cache_lets_calls_through(self) -> None:
        _backed_off()
        provider_health.forget_snapshot()

        unreadable = mock.Mock(get=mock.Mock(side_effect=ConnectionError("down")))
        with background_work(), mock.patch.object(provider_health, "cache", unreadable):
            _session().get(_URL)

    def test_the_refusals_are_counted_for_the_site_admin(self) -> None:
        _backed_off()
        with background_work():
            for _ in range(2):
                with pytest.raises(UpstreamThrottledError):
                    _session().get(_URL)

        self.assertEqual(provider_health.suppressed_today(_SERVICE), 2)


class BackgroundWorkTests(SimpleTestCase):
    def test_only_the_queues_nobody_waits_on_are_background(self) -> None:
        self.assertEqual(
            {queue.value for queue in Queue if queue_is_background(queue)},
            {Queue.BULK.value, Queue.MAINTENANCE.value, Queue.DEFAULT.value, Queue.SANDBOX_BATCH.value},
        )

    def test_work_outside_a_task_is_live(self) -> None:
        self.assertFalse(is_background())

    def test_a_maintenance_task_runs_as_background_work(self) -> None:
        from urbanlens.dashboard.tasks import evaluate_provider_health_task

        seen: list[bool] = []

        def record(**_kwargs: object) -> provider_health.EvaluationReport:
            seen.append(is_background())
            return provider_health.EvaluationReport()

        with mock.patch.object(provider_health, "evaluate_provider_health", side_effect=record):
            evaluate_provider_health_task.apply()

        self.assertEqual(seen, [True])
        self.assertFalse(is_background())


class SiteAdminTests(_GateTestCase):
    def _page(self) -> dict:
        from urbanlens.dashboard.services.admin.site_admin import add_user_to_site_admin_group

        admin = baker.make(User)
        add_user_to_site_admin_group(admin)
        client = Client()
        client.force_login(admin)
        response = client.get(reverse("site_admin_api_limits"))
        self.assertEqual(response.status_code, 200)
        return {"context": response.context["provider_health"], "html": response.content.decode()}

    def test_a_backed_off_provider_is_listed_with_why(self) -> None:
        row = _backed_off()
        ProviderHealth.objects.filter(pk=row.pk).update(
            reason="7 of 40 calls answered in the last hour.", last_evaluated_at=timezone.now()
        )

        page = self._page()

        self.assertEqual([item["row"].provider for item in page["context"]["unhealthy"]], [_SERVICE])
        self.assertIn("7 of 40 calls answered in the last hour.", page["html"])
        self.assertFalse(page["context"]["stale"])

    def test_an_evaluator_that_has_stopped_is_said_to_have_stopped(self) -> None:
        ProviderHealth.objects.create(provider=_SERVICE, last_evaluated_at=timezone.now() - timedelta(hours=2))

        page = self._page()

        self.assertTrue(page["context"]["stale"])
        self.assertIn("has stopped running", page["html"])
