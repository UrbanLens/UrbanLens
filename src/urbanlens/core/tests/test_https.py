"""Tests for HTTPS enforcement via SecurityMiddleware."""

from __future__ import annotations

from django.conf import settings as django_settings
from django.http import HttpResponse
from django.middleware.security import SecurityMiddleware
from django.test import Client, RequestFactory, override_settings

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase


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


class TrustedProxyHeaderTests(SimpleTestCase):
    """Nginx terminates TLS and forwards the real scheme (config/nginx/django.conf).

    Every test above forces ``SECURE_PROXY_SSL_HEADER=None``, which is not the
    deployed value - ``settings/base.py`` sets it to
    ``("HTTP_X_FORWARDED_PROTO", "https")`` so Django trusts Nginx's forwarded
    scheme instead of always seeing the plain-HTTP connection between the two.
    Nothing else in the suite sends that header, so a typo'd header name or
    trusted value here would silently make every request behind the real proxy
    redirect-loop in production while every test above kept passing.
    """

    @override_settings(SECURE_SSL_REDIRECT=True)
    def test_the_deployed_forwarded_proto_header_is_trusted_as_secure(self) -> None:
        header_name, secure_value = django_settings.SECURE_PROXY_SSL_HEADER
        request = RequestFactory().get("/dashboard/", **{header_name: secure_value})

        response = SecurityMiddleware(_ok_response)(request)

        self.assertEqual(response.status_code, 200)


class DockerHealthProbeTests(TestCase):
    """Container healthchecks curl /health/ with Host: localhost."""

    @override_settings(
        ALLOWED_HOSTS=["urbanlens.org"],
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=None,
        SECURE_REDIRECT_EXEMPT=[r"^health"],
    )
    def test_localhost_probe_fails_when_localhost_missing_from_allowed_hosts(self) -> None:
        response = Client(HTTP_HOST="localhost").get("/health/")
        self.assertEqual(response.status_code, 400)

    @override_settings(
        ALLOWED_HOSTS=["urbanlens.org", "localhost", "127.0.0.1"],
        SECURE_SSL_REDIRECT=True,
        SECURE_PROXY_SSL_HEADER=None,
        SECURE_REDIRECT_EXEMPT=[r"^health"],
    )
    def test_localhost_probe_succeeds_for_docker_healthcheck(self) -> None:
        response = Client(HTTP_HOST="localhost").get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"Okay!")
