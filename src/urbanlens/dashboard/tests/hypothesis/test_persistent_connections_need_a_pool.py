"""Keeping a database connection between requests is only safe where a thread gets reused.

``UL_DB_CONN_MAX_AGE`` is set to 300 for the web and websocket containers, which is right for the
servers they run: gunicorn's ``gthread`` pool and Channels' loop both hand the same thread the next
request, so the connection it parked is the connection it picks up. ``runserver`` does not - it
gives each HTTP connection its own thread and drops it - so a parked connection is never reclaimed
by anyone. Measured on the development deployment: 30 idle ``ul_web`` connections with nothing
running, the oldest idle for ten minutes against a max age of five, and a map load failing with
``too many connections for role "ul_web"`` once a viewport of tiles had opened one each.
"""

from __future__ import annotations

import os
from unittest import mock

from django.conf import settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.settings._env import persistent_connection_seconds


class PersistentConnectionPolicyTests(SimpleTestCase):
    """What ``CONN_MAX_AGE`` resolves to, per server."""

    def test_a_pooled_server_keeps_what_the_deployment_asked_for(self) -> None:
        """The setting exists for gunicorn and Channels, and has to keep working there."""
        with mock.patch.dict(os.environ, {"UL_DB_CONN_MAX_AGE": "300"}):
            self.assertEqual(persistent_connection_seconds(argv=["gunicorn", "urbanlens.UrbanLens.wsgi"]), 300)

    def test_runserver_closes_the_connection_with_the_response(self) -> None:
        """A thread per HTTP connection cannot hand a parked connection back, so it must not park one."""
        with mock.patch.dict(os.environ, {"UL_DB_CONN_MAX_AGE": "300"}):
            self.assertEqual(persistent_connection_seconds(argv=["manage.py", "runserver", "0.0.0.0:8000"]), 0)

    def test_an_unset_variable_closes_the_connection(self) -> None:
        """Nobody should have to opt out of a leak."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(persistent_connection_seconds(argv=["gunicorn"]), 0)

    def test_an_unreadable_value_is_not_an_import_error(self) -> None:
        """This is evaluated while settings are being built, where raising takes the process with it."""
        with mock.patch.dict(os.environ, {"UL_DB_CONN_MAX_AGE": "five minutes"}):
            self.assertEqual(persistent_connection_seconds(default=7, argv=["gunicorn"]), 7)

    def test_the_setting_is_wired_to_the_helper(self) -> None:
        """A helper nothing reads would pass every test above and leak in every deployment."""
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], persistent_connection_seconds())
