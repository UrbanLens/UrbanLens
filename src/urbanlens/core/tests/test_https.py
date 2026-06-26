"""Tests for HTTPS enforcement via SecurityMiddleware."""

from __future__ import annotations

from django.http import HttpResponse
from django.middleware.security import SecurityMiddleware
from django.test import RequestFactory, override_settings

from urbanlens.core.tests.testcase import TestCase


def _ok_response(request) -> HttpResponse:
    return HttpResponse("ok")


class HttpsRedirectMiddlewareTests(TestCase):
    """SecurityMiddleware redirects HTTP when SECURE_SSL_REDIRECT is enabled."""

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=None,
        SECURE_REDIRECT_EXEMPT=[],
    )
    def test_http_request_is_redirected_to_https(self) -> None:
        request = RequestFactory().get("/dashboard/")
        response = SecurityMiddleware(_ok_response)(request)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "https://testserver/dashboard/")

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=None,
        SECURE_REDIRECT_EXEMPT=[r"^health"],
    )
    def test_health_path_is_exempt_from_redirect(self) -> None:
        request = RequestFactory().get("/health/")
        response = SecurityMiddleware(_ok_response)(request)
        self.assertEqual(response.status_code, 200)

    @override_settings(
        SECURE_SSL_REDIRECT=False,
        SECURE_PROXY_SSL_HEADER=None,
    )
    def test_http_allowed_when_ssl_redirect_disabled(self) -> None:
        request = RequestFactory().get("/dashboard/")
        response = SecurityMiddleware(_ok_response)(request)
        self.assertEqual(response.status_code, 200)
