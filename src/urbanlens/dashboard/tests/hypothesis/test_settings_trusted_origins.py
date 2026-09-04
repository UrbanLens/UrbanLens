"""CSRF/CORS trusted origins are derived from this deployment's own configuration.

The bug this replaces was quiet in the worst way. ``CSRF_TRUSTED_ORIGINS`` came
from a hardcoded domain list, so an ephemeral dev environment
(``bin/dev_env.py``), served on a generated ``<slug>.dev.urbanlens.org``
hostname, rendered every page perfectly and rejected every POST - login
included - on its Referer. That reads as "the app is broken", not as "this
origin is untrusted", and no hardcoded list can ever enumerate a hostname that
does not exist yet.

Deriving them widens nothing: a host gets here only by already being in
``ALLOWED_HOSTS`` or by being ``UL_SITE_URL``, both of which an operator sets
deliberately per deployment. The tests below pin that boundary - the ``*``
catch-all mints no origin, plain HTTP appears only where it is already allowed,
and junk entries are skipped rather than turned into malformed origins.
"""

from __future__ import annotations

from django.conf import settings as django_settings
from hypothesis import given, strategies as st

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.UrbanLens.settings.base import _derive_trusted_origins, _origin_from_url

_PRODUCTION_HOSTS = ["urbanlens.org", "www.urbanlens.org", "localhost"]


class OriginFromUrlTests(SimpleTestCase):
    def test_a_url_is_reduced_to_its_origin(self) -> None:
        self.assertEqual(
            _origin_from_url("https://a1b2c3.dev.urbanlens.org/accounts/login/"), "https://a1b2c3.dev.urbanlens.org"
        )

    def test_a_port_is_kept_because_an_origin_includes_it(self) -> None:
        self.assertEqual(_origin_from_url("http://localhost:21811/"), "http://localhost:21811")

    def test_an_ipv6_literal_keeps_the_brackets_an_origin_needs(self) -> None:
        """``urlsplit`` strips them; an Origin header does not."""
        self.assertEqual(_origin_from_url("https://[::1]:8000"), "https://[::1]:8000")

    def test_a_non_http_scheme_names_no_origin(self) -> None:
        for url in ("ftp://example.com", "javascript:alert(1)", "example.com", ""):
            with self.subTest(url=url):
                self.assertIsNone(_origin_from_url(url))

    def test_an_unparseable_port_is_skipped_rather_than_guessed(self) -> None:
        self.assertIsNone(_origin_from_url("https://example.com:notaport"))


class DerivedTrustedOriginTests(SimpleTestCase):
    def test_a_host_in_allowed_hosts_becomes_a_trusted_origin(self) -> None:
        exact, wildcard = _derive_trusted_origins(["a1b2c3.dev.urbanlens.org"], "", allow_http=False)

        self.assertEqual(exact, ["https://a1b2c3.dev.urbanlens.org"])
        self.assertEqual(wildcard, [])

    def test_the_site_url_becomes_a_trusted_origin(self) -> None:
        """The generated hostname is already written into every dev environment's .env."""
        exact, _ = _derive_trusted_origins([], "https://a1b2c3.dev.urbanlens.org", allow_http=False)

        self.assertEqual(exact, ["https://a1b2c3.dev.urbanlens.org"])

    def test_a_site_url_with_a_path_still_yields_the_bare_origin(self) -> None:
        exact, _ = _derive_trusted_origins([], "https://example.org/some/page", allow_http=False)

        self.assertEqual(exact, ["https://example.org"])

    def test_the_wildcard_host_mints_nothing(self) -> None:
        """``*`` is a catch-all for the Host header; there is no catch-all origin."""
        exact, wildcard = _derive_trusted_origins(["*", ""], "", allow_http=True)

        self.assertEqual((exact, wildcard), ([], []))

    def test_plain_http_appears_only_where_it_is_already_allowed(self) -> None:
        secure, _ = _derive_trusted_origins(["example.org"], "", allow_http=False)
        insecure, _ = _derive_trusted_origins(["example.org"], "", allow_http=True)

        self.assertEqual(secure, ["https://example.org"])
        self.assertEqual(insecure, ["https://example.org", "http://example.org"])

    def test_a_subdomain_entry_produces_a_csrf_only_wildcard(self) -> None:
        """django-cors-headers rejects a non-URI origin at check time, so it gets the exact form only."""
        exact, wildcard = _derive_trusted_origins([".example.org"], "", allow_http=False)

        self.assertEqual(exact, ["https://example.org"])
        self.assertEqual(wildcard, ["https://*.example.org"])

    def test_a_star_dot_entry_is_read_as_the_same_intent(self) -> None:
        """Django does not accept this spelling in ALLOWED_HOSTS, but operators write it."""
        _, wildcard = _derive_trusted_origins(["*.example.org"], "", allow_http=False)

        self.assertEqual(wildcard, ["https://*.example.org"])

    def test_a_bare_ipv6_literal_is_bracketed(self) -> None:
        exact, _ = _derive_trusted_origins(["::1", "[::1]"], "", allow_http=False)

        self.assertEqual(exact, ["https://[::1]"])

    def test_junk_entries_are_skipped_rather_than_made_into_origins(self) -> None:
        """Read as a URL, each of these would mean something wider than the entry says."""
        exact, wildcard = _derive_trusted_origins(
            ["bad host", "evil.com/x", "user@host", "host:notaport", "*.*"], "", allow_http=True
        )

        self.assertEqual((exact, wildcard), ([], []))

    def test_a_port_in_allowed_hosts_survives_into_the_origin(self) -> None:
        exact, _ = _derive_trusted_origins(["api.example.org:8443"], "", allow_http=False)

        self.assertEqual(exact, ["https://api.example.org:8443"])

    def test_a_production_shaped_deployment_gains_nothing_it_did_not_configure(self) -> None:
        """No dev-only path exists any more: production trusts exactly its own hosts, over HTTPS."""
        exact, wildcard = _derive_trusted_origins(_PRODUCTION_HOSTS, "https://urbanlens.org", allow_http=False)

        self.assertEqual(exact, ["https://urbanlens.org", "https://www.urbanlens.org", "https://localhost"])
        self.assertEqual(wildcard, [])
        self.assertNotIn("https://*.dev.urbanlens.org", exact + wildcard)

    @given(st.lists(st.from_regex(r"\A[a-z][a-z0-9-]{0,20}\.example\.org\Z"), max_size=6), st.booleans())
    def test_every_derived_origin_is_a_scheme_and_a_host_and_nothing_else(
        self, hosts: list[str], allow_http: bool
    ) -> None:
        exact, wildcard = _derive_trusted_origins(hosts, "", allow_http=allow_http)

        for origin in exact + wildcard:
            self.assertRegex(origin, r"^https?://[^\s/?#@]+$")
            if not allow_http:
                self.assertTrue(origin.startswith("https://"), origin)
        self.assertEqual(wildcard, [], "a plain hostname must not produce a wildcard")


class LiveSettingsTests(SimpleTestCase):
    """The wiring, not just the function - a derivation nothing calls fixes nothing."""

    def test_every_plain_allowed_host_is_a_trusted_origin(self) -> None:
        # "testserver" is appended to ALLOWED_HOSTS by Django's own
        # setup_test_environment, long after settings were evaluated, so it is
        # a host the derivation could not have seen - not a gap in it.
        for host in django_settings.ALLOWED_HOSTS:
            if host in {"*", "", "testserver"} or host.startswith((".", "*")) or any(char in host for char in "/@?# "):
                continue
            with self.subTest(host=host):
                self.assertIn(f"https://{host}", django_settings.CSRF_TRUSTED_ORIGINS)

    def test_cors_carries_no_wildcard_origin(self) -> None:
        """django-cors-headers validates its entries as URIs; a ``*.`` form fails that check."""
        for origin in django_settings.CORS_ALLOWED_ORIGINS:
            self.assertNotIn("*", origin)
