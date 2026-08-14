"""A URL that cannot be parsed must not be printed to admins verbatim.

`_redact_url` hides credentials in the service URLs shown on the infrastructure
admin page, and one of its inputs is the Celery broker URL, which embeds a
password. It caught every exception from `urlparse` and returned the *raw* URL,
so the one case it exists to handle - a URL it cannot make sense of - leaked the
credential in full. `urlparse` does raise: a malformed IPv6 literal is a ValueError.
"""

from __future__ import annotations

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.admin.infrastructure_stats import _redact_url

# urlparse raises "Invalid IPv6 URL" on an unclosed bracket in the netloc.
UNPARSEABLE = "redis://user:hunter2@[::1:6379/0"


class RedactUrlTests(SimpleTestCase):
    def test_a_password_is_replaced(self) -> None:
        redacted = _redact_url("redis://user:hunter2@localhost:6379/0")

        self.assertNotIn("hunter2", redacted)
        self.assertIn("user:***", redacted)

    def test_a_url_without_credentials_is_untouched(self) -> None:
        url = "redis://localhost:6379/0"

        self.assertEqual(_redact_url(url), url)

    def test_an_unparseable_url_is_not_returned_verbatim(self) -> None:
        """The regression: this used to return the raw string, password included."""
        with self.assertRaises(ValueError):
            # Guards the fixture itself - if urlparse stops raising here, this test
            # would pass while exercising the ordinary path.
            from urllib.parse import urlparse

            urlparse(UNPARSEABLE)

        redacted = _redact_url(UNPARSEABLE)

        self.assertNotIn("hunter2", redacted)
        self.assertNotIn("user", redacted)
