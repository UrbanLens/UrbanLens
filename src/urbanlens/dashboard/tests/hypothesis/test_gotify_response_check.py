"""A Gotify server that rejects the request must not look like a delivery.

`_send_gotify` caught transport errors but ignored the response status, so a
rotated token (401) or a wrong URL (404) - both of which answer cleanly -
produced no log line at all. Nobody watches for an admin notification that
never arrives, which is exactly why the silent case needed closing.
"""

from __future__ import annotations

from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.notifications.notifications import _send_gotify

_LOGGER = "urbanlens.dashboard.services.notifications.notifications"


class _Site:
    notify_gotify_url = "https://gotify.example.test"
    notify_gotify_token = "token"  # noqa: S105 - a stub attribute, and the name must match the real SiteSettings field


class GotifyResponseCheckTests(SimpleTestCase):
    def test_a_rejected_token_is_logged_as_an_error(self) -> None:
        with mock.patch("requests.post", return_value=mock.Mock(ok=False, status_code=401)), self.assertLogs(_LOGGER, level="ERROR") as logs:
            _send_gotify(_Site(), "subject", "message")
        self.assertTrue(any("401" in line for line in logs.output))

    def test_a_successful_send_logs_no_error(self) -> None:
        with mock.patch("requests.post", return_value=mock.Mock(ok=True, status_code=200)), mock.patch.object(__import__("logging").getLogger(_LOGGER), "error") as error:
            _send_gotify(_Site(), "subject", "message")
        error.assert_not_called()

    def test_a_transport_failure_still_returns_quietly(self) -> None:
        """An unreachable Gotify must not propagate into whatever raised the notification."""
        import requests

        with mock.patch("requests.post", side_effect=requests.RequestException("down")), self.assertLogs(_LOGGER, level="ERROR"):
            _send_gotify(_Site(), "subject", "message")
