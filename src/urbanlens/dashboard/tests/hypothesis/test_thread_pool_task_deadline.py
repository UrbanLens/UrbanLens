"""A task's soft limit holds on a thread pool, where no signal can deliver it (P179).

``celery-worker-panels`` runs ``--pool=threads``: billiard's soft-limit signal only reaches a prefork child's main
thread, so a ``panel_fetch`` task's declared limits did nothing there, and a hung upstream call held its thread
until the HTTP client's own timeout.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core import rate_limiter, task_limits
from urbanlens.dashboard.services.core.timeout_utils import submit_bounded
from urbanlens.UrbanLens.celery import app as celery_app


def _in_a_worker_thread(func):
    """Run ``func`` off the main thread, as the threads pool does, and return or raise what it did."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test thread
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


class _Base(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sent: list[object] = []
        for name, value in (("_reserve_call", 1), ("_finalize_call", None)):
            patcher = mock.patch.object(rate_limiter, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("requests.Session.request", side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, method, url, **kwargs):
        self.sent.append(kwargs.get("timeout"))
        return mock.Mock(ok=True, status_code=200)

    def _task(self, body, soft: int = 2):
        return celery_app.task(
            name=f"urbanlens.probe.deadline.{body.__name__}",
            base=task_limits.UrbanLensTask,
            soft_time_limit=soft,
            time_limit=soft + 1,
        )(body)

    def _session(self):
        return rate_limiter._RateLimitedSession("probe-service")


class SoftLimitOnAThreadPoolTests(_Base):
    def test_a_call_after_the_soft_limit_is_refused_and_the_task_fails(self) -> None:
        session = self._session()

        def overruns():
            time.sleep(1.2)
            session.get("https://upstream.example/slow")

        with self.assertRaises(SoftTimeLimitExceeded):
            _in_a_worker_thread(self._task(overruns, soft=1))
        self.assertEqual(self.sent, [], "a request went out after the task's soft limit")

    def test_a_broad_handler_does_not_swallow_it(self) -> None:
        session = self._session()
        carried_on: list[bool] = []

        def swallows():
            time.sleep(1.2)
            try:
                session.get("https://upstream.example/slow")
            except Exception:
                carried_on.append(True)
            carried_on.append(True)

        with self.assertRaises(SoftTimeLimitExceeded):
            _in_a_worker_thread(self._task(swallows, soft=1))
        self.assertEqual(carried_on, [])

    def test_a_call_cannot_wait_past_the_soft_limit(self) -> None:
        session = self._session()

        def asks():
            session.get("https://upstream.example/slow", timeout=(10, 60))
            session.get("https://upstream.example/slow", timeout=60)
            session.get("https://upstream.example/slow")

        _in_a_worker_thread(self._task(asks, soft=3))

        self.assertEqual(len(self.sent), 3)
        for timeout in self.sent:
            phases = timeout if isinstance(timeout, tuple) else (timeout,)
            self.assertTrue(all(phase is not None and 0 < phase <= 3 for phase in phases), timeout)

    def test_a_task_called_inside_a_task_cannot_extend_the_outer_limit(self) -> None:
        session = self._session()

        def inner_body():
            session.get("https://upstream.example/inner")

        inner = self._task(inner_body, soft=100)

        def outer_body():
            inner()

        _in_a_worker_thread(self._task(outer_body, soft=3))

        [timeout] = self.sent
        self.assertLessEqual(max(timeout), 3)

    def test_the_deadline_follows_a_call_onto_the_deadline_pool(self) -> None:
        session = self._session()

        def fans_out():
            return submit_bounded(lambda: session.get("https://upstream.example/elsewhere")).result(5)

        _in_a_worker_thread(self._task(fans_out, soft=3))

        [timeout] = self.sent
        self.assertLessEqual(max(timeout), 3)

    def test_waiting_on_a_slow_call_ends_at_the_soft_limit(self) -> None:
        from urbanlens.dashboard.services.core.timeout_utils import call_with_deadline

        def waits():
            return call_with_deadline(lambda: time.sleep(4), timeout=30, default="gave up", name="probe")

        started = time.monotonic()
        with self.assertRaises(SoftTimeLimitExceeded):
            _in_a_worker_thread(self._task(waits, soft=1))
        self.assertLess(time.monotonic() - started, 3)


class OutsideATaskTests(_Base):
    def test_a_request_handler_keeps_its_own_timeout(self) -> None:
        self._session().get("https://upstream.example/page", timeout=(5, 30))
        self.assertEqual(self.sent, [(5, 30)])

    def test_the_deadline_ends_with_the_task(self) -> None:
        session = self._session()

        def returns():
            return None

        _in_a_worker_thread(self._task(returns, soft=1))
        time.sleep(1.1)
        session.get("https://upstream.example/after")
        self.assertEqual(self.sent, [(5, 30)])
