"""The transport-security settings that protect every response.

Most of these are Django defaults rather than explicit settings, which is fine
until someone changes one. Asserting them here means a regression shows up as a
failing test rather than as a quietly weaker deployment, and it documents which
ones are deliberate.

HSTS is the one that was actually missing. ``SECURE_SSL_REDIRECT`` alone does
not close the gap it covers: the redirect is itself served over plain HTTP, so
an attacker on the path can answer it instead of letting it reach the user. HSTS
is what stops the *second* visit from being strippable.
"""

from __future__ import annotations

from django.conf import settings
from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase


class TransportSecuritySettingTests(SimpleTestCase):
    def test_hsts_is_tied_to_the_ssl_redirect_gate(self) -> None:
        """One switch, not two: an HTTP-only deployment must not advertise HSTS.

        Sending HSTS from a deployment intentionally served over HTTP makes it
        unreachable in any browser that has seen the header, and it is not
        promptly reversible.
        """
        if settings.SECURE_SSL_REDIRECT:
            self.assertGreater(settings.SECURE_HSTS_SECONDS, 0)
        else:
            self.assertEqual(settings.SECURE_HSTS_SECONDS, 0)

    def test_preload_is_not_enabled_by_default(self) -> None:
        """Preload submission is the domain owner's call and is painful to undo."""
        self.assertFalse(getattr(settings, "SECURE_HSTS_PRELOAD", False))

    def test_cookies_are_never_weaker_than_the_tls_gate(self) -> None:
        """One-directional, not equality.

        Both settings default to ``SECURE_SSL_REDIRECT`` but are explicitly
        overridable (``_env_bool("SESSION_COOKIE_SECURE", ...)``), and marking
        cookies secure on a deployment that also permits HTTP is *stricter*, not
        weaker - this very environment does exactly that. What must never happen
        is the reverse: HTTPS enforced while cookies are still sent in the clear.
        """
        if settings.SECURE_SSL_REDIRECT:
            self.assertTrue(settings.SESSION_COOKIE_SECURE)
            self.assertTrue(settings.CSRF_COOKIE_SECURE)

    def test_session_cookie_is_http_only(self) -> None:
        """The CSRF cookie deliberately is not - the frontend reads it."""
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    def test_content_type_sniffing_is_disabled(self) -> None:
        """Matters most for the media gate, which serves user-supplied bytes."""
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)

    def test_framing_is_denied(self) -> None:
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")

    def test_referrer_policy_does_not_leak_urls_cross_origin(self) -> None:
        """Pin and share URLs are themselves sensitive - they identify a location."""
        policy = settings.SECURE_REFERRER_POLICY
        allowed = {"same-origin", "no-referrer", "strict-origin", "strict-origin-when-cross-origin"}
        values = {policy} if isinstance(policy, str) else set(policy)

        self.assertTrue(values <= allowed, f"referrer policy {policy!r} may send full URLs cross-origin")


class HstsGateTests(SimpleTestCase):
    """The gate itself, exercised both ways rather than only in this environment."""

    @override_settings(SECURE_SSL_REDIRECT=False, SECURE_HSTS_SECONDS=0)
    def test_http_only_deployment_sends_no_hsts(self) -> None:
        response = self.client.get("/health/")

        self.assertNotIn("Strict-Transport-Security", response.headers)

    @override_settings(SECURE_SSL_REDIRECT=False, SECURE_HSTS_SECONDS=31536000, SECURE_HSTS_INCLUDE_SUBDOMAINS=True)
    def test_configured_seconds_reach_the_response(self) -> None:
        """SecurityMiddleware only emits the header on a secure request."""
        response = self.client.get("/health/", secure=True)

        self.assertIn("max-age=31536000", response.headers.get("Strict-Transport-Security", ""))
        self.assertIn("includeSubDomains", response.headers.get("Strict-Transport-Security", ""))
