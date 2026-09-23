"""Violation reports from the enforced Content-Security-Policy land in the log, and nowhere they can hurt."""

from __future__ import annotations

import json

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.controllers.csp_report import CSP_REPORT_RATE, MAX_REPORT_BYTES
from urbanlens.dashboard.tests.hypothesis.test_security_headers import parse_csp

LOGGER = "urbanlens.dashboard.controllers.csp_report"

LEGACY_REPORT = {
    "csp-report": {
        "document-uri": "https://urbanlens.org/oauth/authorize/?code=secret-code&state=s",
        "effective-directive": "form-action",
        "violated-directive": "form-action",
        "blocked-uri": "https://accounts.google.com/o/oauth2/auth?client_id=x&state=abc",
        "source-file": "https://unpkg.com/htmx.org@1.9.11?x=1",
        "line-number": 1,
        "disposition": "enforce",
    },
}


class CspReportEndpointTests(SimpleTestCase):
    def setUp(self) -> None:
        cache.clear()
        self.url = reverse("csp.report")

    def _post(self, body: object, content_type: str = "application/csp-report") -> int:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self.client.post(self.url, payload, content_type=content_type).status_code

    def test_the_policy_sends_its_reports_here(self) -> None:
        policy = parse_csp(self.client.get("/health/").headers["Content-Security-Policy"])

        self.assertEqual(policy["report-uri"], [self.url])

    def test_a_legacy_report_is_logged_without_query_strings(self) -> None:
        """A document URL can carry an OAuth code or a share token; the log must not."""
        with self.assertLogs(LOGGER, "WARNING") as logs:
            status = self._post(LEGACY_REPORT)

        self.assertEqual(status, 204)
        line = "\n".join(logs.output)
        self.assertIn("form-action", line)
        self.assertIn("https://accounts.google.com/o/oauth2/auth", line)
        self.assertIn("/oauth/authorize/", line)
        self.assertNotIn("secret-code", line)
        self.assertNotIn("state=", line)
        self.assertNotIn("client_id", line)

    def test_a_reporting_api_batch_is_logged_per_report(self) -> None:
        batch = [
            {
                "type": "csp-violation",
                "url": "https://urbanlens.org/dashboard/map/",
                "body": {
                    "effectiveDirective": "worker-src",
                    "blockedURL": "blob",
                    "documentURL": "https://urbanlens.org/dashboard/map/",
                    "disposition": "enforce",
                },
            },
            {"type": "deprecation", "body": {"id": "x"}},
            {
                "type": "csp-violation",
                "body": {
                    "effectiveDirective": "script-src-elem",
                    "blockedURL": "https://evil.example/x.js",
                    "documentURL": "https://urbanlens.org/",
                },
            },
        ]
        with self.assertLogs(LOGGER, "WARNING") as logs:
            status = self._post(batch, content_type="application/reports+json")

        self.assertEqual(status, 204)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("worker-src", logs.output[0])
        self.assertIn("https://evil.example/x.js", logs.output[1])

    def test_a_report_cannot_forge_log_lines(self) -> None:
        forged = {
            "csp-report": {
                **LEGACY_REPORT["csp-report"],
                "effective-directive": "script-src\nERROR admin password reset",
            }
        }
        with self.assertLogs(LOGGER, "WARNING") as logs:
            self._post(forged)

        self.assertNotIn("\n", logs.records[0].getMessage())

    def test_a_long_field_is_truncated(self) -> None:
        huge = {"csp-report": {**LEGACY_REPORT["csp-report"], "blocked-uri": "https://x.example/" + "a" * 5000}}
        with self.assertLogs(LOGGER, "WARNING") as logs:
            self._post(huge)

        self.assertLess(len(logs.records[0].getMessage()), 1200)

    def test_an_oversized_body_is_refused_unread(self) -> None:
        status = self._post(b"x" * (MAX_REPORT_BYTES + 1))

        self.assertEqual(status, 413)

    def test_a_body_that_is_not_a_report_is_refused_quietly(self) -> None:
        for body in (b"not json", b"[]", b'{"csp-report": "x"}', b"42"):
            with self.subTest(body=body):
                status = self._post(body)
                self.assertIn(status, {204, 400})

    def test_only_post_is_accepted(self) -> None:
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_flood_from_one_address_is_throttled(self) -> None:
        with self.assertLogs(LOGGER, "WARNING"):
            statuses = [self._post(LEGACY_REPORT) for _ in range(CSP_REPORT_RATE.limit + 1)]

        self.assertEqual(statuses[:-1], [204] * CSP_REPORT_RATE.limit)
        self.assertEqual(statuses[-1], 429)

    def test_the_endpoint_needs_no_csrf_token(self) -> None:
        """Browsers send reports with no cookie-bound token; CSRF would drop every one."""
        self.assertTrue(settings.CSP_ENFORCE)
        client = self.client_class(enforce_csrf_checks=True)
        with self.assertLogs(LOGGER, "WARNING"):
            response = client.post(self.url, json.dumps(LEGACY_REPORT), content_type="application/csp-report")

        self.assertEqual(response.status_code, 204)
