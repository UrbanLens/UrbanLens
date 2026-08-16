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

from pathlib import Path
import re

from django.conf import settings
from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase

#: ``dashboard/templates/dashboard/themes/base.html`` - the template every page extends.
BASE_TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "dashboard" / "themes" / "base.html"


def parse_csp(header_value: str) -> dict[str, list[str]]:
    """Split a CSP header into ``{directive: [source, ...]}``.

    Args:
        header_value: The raw header value, e.g. ``"default-src 'self'; object-src 'none'"``.

    Returns:
        One entry per directive. Valueless directives (``upgrade-insecure-requests``)
        map to an empty list.
    """
    directives: dict[str, list[str]] = {}
    for chunk in header_value.split(";"):
        parts = chunk.split()
        if parts:
            directives[parts[0]] = parts[1:]
    return directives


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


ENFORCE_HEADER = "Content-Security-Policy"
REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"


class CspHeaderTests(SimpleTestCase):
    """The Content-Security-Policy actually reaching responses.

    The policy ships report-only, so these assert the header is *present and
    correct* rather than that anything is blocked - a report-only policy blocks
    nothing by design.
    """

    def test_middleware_is_installed_directly_below_securitymiddleware(self) -> None:
        """Order matters: the nonce must exist before any view can read it."""
        self.assertIn("csp.middleware.CSPMiddleware", settings.MIDDLEWARE)
        self.assertEqual(
            settings.MIDDLEWARE.index("csp.middleware.CSPMiddleware"),
            settings.MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,
        )

    def test_report_only_is_the_default(self) -> None:
        """Enforcing a first policy blind is how a CSP breaks a site quietly."""
        self.assertFalse(settings.CSP_ENFORCE)

        response = self.client.get("/health/")

        self.assertIn(REPORT_ONLY_HEADER, response.headers)
        self.assertNotIn(ENFORCE_HEADER, response.headers)

    def test_hardening_directives_are_present(self) -> None:
        """The four directives that cost nothing and close real classes of attack.

        ``object-src 'none'`` kills plugin-based script execution, ``base-uri``
        stops an injected ``<base>`` from repointing every relative URL,
        ``frame-ancestors`` covers clickjacking and ``form-action`` stops an
        injected form from posting credentials off-site.
        """
        response = self.client.get("/health/")
        policy = parse_csp(response.headers[REPORT_ONLY_HEADER])

        self.assertEqual(policy["object-src"], ["'none'"])
        self.assertEqual(policy["base-uri"], ["'self'"])
        self.assertEqual(policy["form-action"], ["'self'"])
        self.assertIn("frame-ancestors", policy)
        self.assertEqual(policy["default-src"], ["'self'"])

    def test_enforce_toggle_switches_which_header_is_sent(self) -> None:
        """``UL_CSP_ENFORCE`` decides the header name, not the policy content."""
        directives = settings.CONTENT_SECURITY_POLICY_REPORT_ONLY["DIRECTIVES"]

        with override_settings(
            CSP_ENFORCE=True,
            CONTENT_SECURITY_POLICY={"DIRECTIVES": directives},
            CONTENT_SECURITY_POLICY_REPORT_ONLY=None,
        ):
            response = self.client.get("/health/")

        self.assertIn(ENFORCE_HEADER, response.headers)
        self.assertNotIn(REPORT_ONLY_HEADER, response.headers)

        enforced = parse_csp(response.headers[ENFORCE_HEADER])
        self.assertEqual(enforced["object-src"], ["'none'"])
        self.assertEqual(enforced["form-action"], ["'self'"])

    def test_no_unsafe_eval_anywhere(self) -> None:
        """'unsafe-inline' is a deliberate concession; 'unsafe-eval' is not."""
        response = self.client.get("/health/")

        self.assertNotIn("'unsafe-eval'", response.headers[REPORT_ONLY_HEADER])


class CspMatchesTheTemplatesTests(SimpleTestCase):
    """Guards the policy against drifting away from what the pages actually load.

    These read the base template rather than hardcoding a host list, so they keep
    working as the frontend changes: tightening the policy while the templates
    still need a host fails here instead of in production.
    """

    def test_every_cdn_script_host_in_the_base_template_is_allowed(self) -> None:
        """A missing host here is a blank page once the policy is enforced."""
        html = BASE_TEMPLATE.read_text(encoding="utf-8")
        hosts = set(re.findall(r"""<script[^>]+src=["'](https://[^/"']+)""", html, re.IGNORECASE))
        self.assertTrue(hosts, f"expected remote <script> tags in {BASE_TEMPLATE}")

        allowed = settings.CONTENT_SECURITY_POLICY_REPORT_ONLY["DIRECTIVES"]["script-src"]

        for host in hosts:
            self.assertIn(host, allowed, f"{host} is loaded by base.html but missing from script-src")

    def test_unsafe_inline_is_kept_while_inline_scripts_remain(self) -> None:
        """The caveat, pinned to the thing that causes it.

        ``'unsafe-inline'`` cannot be dropped until the inline blocks are gone -
        and a nonce would not help incrementally, since browsers ignore
        ``'unsafe-inline'`` as soon as any nonce is present. Once the inline-JS
        extraction work lands and base.html has no inline blocks left, this test
        stops requiring the concession instead of having to be deleted.
        """
        html = BASE_TEMPLATE.read_text(encoding="utf-8")
        # Case-insensitive: HTML tag names are, so a <SCRIPT> block would
        # otherwise slip past and read as "no inline scripts left".
        inline_blocks = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>", html, re.IGNORECASE)

        script_src = settings.CONTENT_SECURITY_POLICY_REPORT_ONLY["DIRECTIVES"]["script-src"]

        if inline_blocks:
            self.assertIn(
                "'unsafe-inline'",
                script_src,
                f"base.html still has {len(inline_blocks)} inline <script> block(s); removing "
                "'unsafe-inline' would break them",
            )
