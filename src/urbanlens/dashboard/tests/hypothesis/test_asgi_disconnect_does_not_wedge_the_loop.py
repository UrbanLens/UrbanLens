"""A request cancelled mid-flight must not freeze the ASGI server.

daphne cancels an application task it considers abandoned. If that lands before Django has handled
the ``http.disconnect``, Django's handler leaves its request task running and exits
``ThreadSensitiveContext``, whose exit joins the request's thread on the event loop. With a sync
middleware in front of the async view handler, that thread is parked in ``async_to_sync`` waiting
for the very loop the join blocks. On the dev stack this froze daphne for over an hour: 163 threads
parked in ``WriteSourceMiddleware`` and every ``ul_web`` connection held.

Each scenario runs in its own subprocess, so a deadlock fails the test rather than hanging the suite.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import sys
import textwrap
import threading
import time

from django.http import HttpRequest, HttpResponse
from django.test import override_settings
from django.urls import path

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens import asgi

VIEW_SECONDS = 0.6
DEADLOCK_SECONDS = 5.0
REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]

_middleware_returned = threading.Event()


def _slow_view(request: HttpRequest) -> HttpResponse:
    time.sleep(VIEW_SECONDS)
    return HttpResponse("ok")


urlpatterns = [path("slow/", _slow_view)]


class _SyncMiddleware:
    """A plain sync middleware, which Django bridges to the async view handler with ``async_to_sync``."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        try:
            return self.get_response(request)
        finally:
            _middleware_returned.set()


def _scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/slow/",
        "raw_path": b"/slow/",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _cancel_then_disconnect(handler) -> dict[str, float]:
    """Cancel the application task while the view runs, then hang up, and time the loop's worst stall."""
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue = asyncio.Queue()
    await inbox.put({"type": "http.request", "body": b"", "more_body": False})

    async def send(message: dict) -> None:
        return None

    application = asyncio.create_task(handler(_scope(), inbox.get, send))
    await asyncio.sleep(VIEW_SECONDS / 12)
    application.cancel()
    await asyncio.sleep(VIEW_SECONDS / 12)
    await inbox.put({"type": "http.disconnect"})

    worst_tick = 0.0
    watch_until = loop.time() + VIEW_SECONDS
    while loop.time() < watch_until:
        before = loop.time()
        await asyncio.sleep(0.01)
        worst_tick = max(worst_tick, loop.time() - before)
    # A real server's loop outlives the request, so keep this one alive until the thread unwinds.
    returned = await asyncio.to_thread(_middleware_returned.wait, DEADLOCK_SECONDS)
    return {"worst_tick": worst_tick, "middleware_returned": float(returned)}


def run_isolated(handler) -> dict[str, float] | None:
    """Run the scenario in a daemon thread; ``None`` means the loop never came back."""
    outcome: dict[str, dict[str, float]] = {}

    def target() -> None:
        outcome["result"] = asyncio.run(_cancel_then_disconnect(handler))

    runner = threading.Thread(target=target, daemon=True)
    runner.start()
    runner.join(DEADLOCK_SECONDS * 2)
    return outcome.get("result")


def handler_under(handler_class: type):
    """Build a handler whose middleware chain is the single sync middleware above."""
    with override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=[f"{__name__}._SyncMiddleware"]):
        return handler_class()


def _probe(handler_class: str) -> str:
    """Run the scenario against ``module:Class`` in a subprocess and report its outcome as one line.

    A subprocess, because a wedged request thread belongs to a ThreadPoolExecutor and the
    interpreter joins those at exit - in-process, a regression would hang the suite, not fail it.
    """
    module, _, name = handler_class.partition(":")
    source = textwrap.dedent(
        f"""
        import json, os, sys
        sys.path.insert(0, {str(REPO_ROOT / "src")!r})
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "urbanlens.UrbanLens.settings.test")
        import django
        django.setup()
        from {module} import {name} as handler_class
        from {__name__} import handler_under, run_isolated
        result = run_isolated(handler_under(handler_class))
        print("DEADLOCK" if result is None else json.dumps(result), flush=True)
        os._exit(0)
        """,
    )
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, timeout=300, cwd=str(REPO_ROOT), check=False
    )
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise AssertionError(f"the probe printed nothing:\n{result.stderr[-2000:]}")
    return lines[-1]


class CancelledRequestTests(SimpleTestCase):
    """The project's HTTP handler survives a request cancelled while its view is running."""

    def test_the_loop_keeps_answering_and_the_request_thread_unwinds(self) -> None:
        handler_class = type(asgi.django_asgi_app)
        outcome = _probe(f"{handler_class.__module__}:{handler_class.__qualname__}")

        self.assertNotEqual(outcome, "DEADLOCK", "a cancelled request deadlocked the event loop")
        result = json.loads(outcome)
        self.assertTrue(result["middleware_returned"], "the cancelled request's thread never unwound")
        self.assertLess(
            result["worst_tick"],
            VIEW_SECONDS / 2,
            "the event loop stalled while a cancelled request's view was still running",
        )

    def test_the_scenario_deadlocks_djangos_own_handler(self) -> None:
        """Anti-vacuity: the same scenario must wedge the stock handler, or the test above proves nothing."""
        self.assertEqual(
            _probe("django.core.handlers.asgi:ASGIHandler"),
            "DEADLOCK",
            "Django's stock handler survived this scenario - if asgiref or Django fixed the blocking exit "
            "upstream, urbanlens.core.asgi can go",
        )
