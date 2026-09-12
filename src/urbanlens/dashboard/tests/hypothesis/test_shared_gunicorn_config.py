"""`gunicorn.conf.py` is loaded by every gunicorn in the image, not just the app.

Gunicorn reads `gunicorn.conf.py` from the working directory whether or not
`-c` names it, and the working directory is `/app` for every service built from
this image. So the config written for the main Django app is also applied to
`ai-inference`, which runs a different WSGI application (`urbanlens_ai`) with no
Django, no ORM and no `DJANGO_SETTINGS_MODULE` - the isolation being the point
of that service (docs/AI_PIPELINE.md).

Two consequences, both visible in staging's boot log:

* `post_fork` applies psycogreen's **gevent** patch inside a **gthread**
  worker. It is inert there only because that service never opens a database
  connection, which is a property of today's AI app rather than of the hook.
  The same hook is what D11 has to get right when the main app moves to
  gthread, so the guard belongs on the worker class either way.
* `post_worker_init` calls `django.setup()` and logs a full
  `ImproperlyConfigured` traceback on every boot, for a service that correctly
  has no Django settings. A warm-up that cannot apply should be silent, not
  alarming - a log that cries wolf every restart is one nobody reads.

Read off the module rather than off a running gunicorn: the hooks take an
arbiter and a worker, which is exactly what a fake can supply.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
from types import SimpleNamespace
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase

REPO_ROOT = pathlib.Path(__file__).resolve().parents[5]


def _load_config():
    """Import `gunicorn.conf.py` by path - it is not on the import path."""
    spec = importlib.util.spec_from_file_location("_gunicorn_conf_under_test", REPO_ROOT / "gunicorn.conf.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _worker():
    """A worker whose log records calls instead of writing them."""
    return SimpleNamespace(pid=1234, log=mock.Mock())


def _server(worker_class: str):
    """An arbiter reporting the worker class it was started with."""
    return SimpleNamespace(cfg=SimpleNamespace(worker_class_str=worker_class), log=mock.Mock())


class ThePsycopgPatchFollowsTheWorkerClassTests(SimpleTestCase):
    """psycogreen registers a *gevent* wait callback on psycopg2. Under any
    other worker class there is no hub to yield to, so the patch is at best
    pointless and at worst a database call waiting on a loop nobody runs."""

    def setUp(self) -> None:
        super().setUp()
        self.config = _load_config()
        self.patch = mock.patch("psycogreen.gevent.patch_psycopg")
        self.patched = self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_a_gevent_worker_is_patched(self) -> None:
        """Anti-vacuity: the guard must not simply switch the hook off."""
        self.config.post_fork(_server("gevent"), _worker())

        self.assertEqual(self.patched.call_count, 1)

    def test_a_threaded_worker_is_not_patched(self) -> None:
        self.config.post_fork(_server("gthread"), _worker())

        self.assertEqual(self.patched.call_count, 0)

    def test_a_sync_worker_is_not_patched(self) -> None:
        self.config.post_fork(_server("sync"), _worker())

        self.assertEqual(self.patched.call_count, 0)


class TheWarmUpSkipsProcessesItCannotWarmTests(SimpleTestCase):
    """The URLconf warm exists to move a 9.5s import off the first request. A
    process with no Django settings has no URLconf to warm, and saying so with
    a traceback misreports a correct configuration as a failure.

    Asserted against `django.setup` rather than against the absence of a logged
    traceback: the test process has Django configured already, so `setup()`
    succeeds here whatever the environment says, and a test watching only for
    the exception would pass against the unfixed hook. It did.
    """

    def setUp(self) -> None:
        super().setUp()
        self.config = _load_config()
        self.patch = mock.patch("django.setup")
        self.setup = self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_a_process_without_django_settings_is_left_alone(self) -> None:
        worker = _worker()
        with mock.patch.dict(os.environ):
            os.environ.pop("DJANGO_SETTINGS_MODULE", None)
            self.config.post_worker_init(worker)

        self.setup.assert_not_called()
        worker.log.exception.assert_not_called()

    def test_the_django_app_is_still_warmed(self) -> None:
        """Anti-vacuity: a guard that skipped every process would pass the test
        above while removing the thing the hook is for."""
        worker = _worker()
        with mock.patch.dict(os.environ, {"DJANGO_SETTINGS_MODULE": "urbanlens.UrbanLens.settings"}):
            self.config.post_worker_init(worker)

        self.setup.assert_called_once()
        worker.log.exception.assert_not_called()
        self.assertTrue(
            any("URLconf warmed" in str(call) for call in worker.log.info.call_args_list),
            "the Django app was not warmed",
        )
