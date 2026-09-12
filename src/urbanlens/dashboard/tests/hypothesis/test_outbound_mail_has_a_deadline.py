"""Mail is sent on the request path, and nothing bounds how long a send may take.

H51's other half, and the half `throttled(...)` does not reach. That decorator
caps how *often* an anonymous caller may hit signup, resend-verification and
password reset. A rate limit bounds frequency, not duration: with
``EMAIL_TIMEOUT`` unset Django passes no ``timeout`` to ``smtplib`` at all, so
the socket inherits the process-wide default and blocks for as long as the peer
stays silent.

The two compose badly rather than cancelling. Ten permitted calls per IP against
a black-holed mail server is ten workers held indefinitely, and the identity is
an IP address, so the ten is per attacker rather than in total. A mail server
that hangs rather than refuses is the ordinary failure - a filtered port, a
provider throttling a sender, DNS pointing somewhere that swallows packets - so
this does not need an attacker at all.

The assertion that matters is the last one: the deadline has to reach the
constructor that opens the socket. A setting that exists but is never passed
down reads exactly like a fix and bounds nothing.
"""

from __future__ import annotations

import math
from unittest import mock

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend
from django.test import override_settings

from urbanlens.core.tests.testcase import TestCase

SETTING = "EMAIL_TIMEOUT"


class TheDeadlineIsConfiguredTests(TestCase):
    def test_the_deadline_is_a_real_setting(self) -> None:
        """Overriding a name production lacks would pass against unbounded code."""
        self.assertTrue(hasattr(settings, SETTING), f"nothing defines {SETTING}")

    def test_the_deadline_is_a_finite_positive_number(self) -> None:
        value = getattr(settings, SETTING, None)

        self.assertIsNotNone(value, "EMAIL_TIMEOUT is None, so smtplib blocks forever")
        self.assertGreater(value, 0)
        self.assertTrue(math.isfinite(value))

    def test_the_deadline_is_short_enough_to_be_a_bound(self) -> None:
        """The anti-vacuity half: a deadline of an hour is not a deadline."""
        self.assertLessEqual(settings.EMAIL_TIMEOUT, 60)


class TheSocketReceivesTheDeadlineTests(TestCase):
    """A setting only bounds anything if it reaches the thing that opens the socket."""

    def _opened_with(self, connection_class: str) -> dict:
        with mock.patch(f"smtplib.{connection_class}") as constructor:
            backend = EmailBackend()
            backend.open()

        self.assertTrue(constructor.called, f"the backend never constructed smtplib.{connection_class}")
        return constructor.call_args.kwargs

    def test_the_smtp_backend_passes_a_timeout_to_the_socket(self) -> None:
        kwargs = self._opened_with("SMTP")

        self.assertIn("timeout", kwargs, "no timeout reached smtplib; the socket blocks forever")
        self.assertEqual(kwargs["timeout"], settings.EMAIL_TIMEOUT)

    @override_settings(EMAIL_USE_SSL=True, EMAIL_USE_TLS=False)
    def test_the_ssl_backend_passes_a_timeout_too(self) -> None:
        """Capping one connection class only moves the problem to the other."""
        kwargs = self._opened_with("SMTP_SSL")

        self.assertIn("timeout", kwargs)
        self.assertEqual(kwargs["timeout"], settings.EMAIL_TIMEOUT)

    @override_settings(EMAIL_TIMEOUT=7)
    def test_the_deadline_is_read_at_construction_not_frozen_at_import(self) -> None:
        """A module constant bound as a default is inert - configurable only in appearance."""
        kwargs = self._opened_with("SMTP")

        self.assertEqual(kwargs["timeout"], 7)
