"""A script's request after the session has ended is refused, not redirected to the login page (P192).

``fetch()`` and htmx follow a redirect, so the login page came back as a 200 and every caller that checks only the
status ran its success path: the edit was not saved and the page said it was.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard import middleware
from urbanlens.dashboard.middleware import SESSION_ENDED_MESSAGE, ScriptLoginRefusalMiddleware
from urbanlens.dashboard.models.pin.model import Pin


class ExpiredSessionScriptRequestTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        owner = baker.make(User)
        self.pin = baker.make(Pin, profile=owner.profile, name="Old mill", parent_pin=None)
        self.edit_url = reverse("pin.edit", kwargs={"pin_slug": self.pin.slug})

    def assert_refused(self, response) -> None:
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("Location", response)
        self.assertIn("sign in", response.content.decode().lower())
        self.assertTrue(response["Content-Type"].startswith("text/plain"))

    def assert_sent_to_login(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertEqual(urlsplit(response["Location"]).path, settings.LOGIN_URL)

    def test_a_fetch_write_is_401_and_writes_nothing(self) -> None:
        response = self.client.post(self.edit_url, {"name": "New mill"}, headers={"Sec-Fetch-Mode": "cors"})

        self.assert_refused(response)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Old mill")

    def test_a_same_origin_fetch_is_401(self) -> None:
        self.assert_refused(self.client.post(self.edit_url, {"name": "x"}, headers={"Sec-Fetch-Mode": "same-origin"}))

    def test_an_htmx_request_is_401_without_fetch_metadata(self) -> None:
        self.assert_refused(self.client.post(self.edit_url, {"name": "x"}, headers={"HX-Request": "true"}))

    def test_an_xhr_marked_request_is_401(self) -> None:
        self.assert_refused(
            self.client.get(reverse("settings.geocode"), headers={"X-Requested-With": "XMLHttpRequest"})
        )

    def test_a_login_required_function_view_is_covered(self) -> None:
        self.assert_refused(self.client.get(reverse("settings.geocode"), headers={"Sec-Fetch-Mode": "cors"}))

    def test_a_navigation_still_goes_to_the_login_page(self) -> None:
        self.assert_sent_to_login(self.client.get(self.edit_url, headers={"Sec-Fetch-Mode": "navigate"}))

    def test_a_form_post_navigation_still_goes_to_the_login_page(self) -> None:
        self.assert_sent_to_login(
            self.client.post(self.edit_url, {"name": "x"}, headers={"Sec-Fetch-Mode": "navigate"})
        )

    def test_a_request_without_fetch_metadata_still_goes_to_the_login_page(self) -> None:
        self.assert_sent_to_login(self.client.get(self.edit_url))

    def test_a_signed_in_script_request_is_unaffected(self) -> None:
        self.client.force_login(self.pin.profile.user)

        response = self.client.post(self.edit_url, {"name": "New mill"}, headers={"Sec-Fetch-Mode": "cors"})

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "New mill")


class ScriptLoginRefusalMiddlewareTests(SimpleTestCase):
    def respond(self, location: str, *, signed_in: bool = False) -> int:
        request = RequestFactory().get("/dashboard/x/", headers={"Sec-Fetch-Mode": "cors"})
        request.user = User(pk=1) if signed_in else AnonymousUser()
        return ScriptLoginRefusalMiddleware(lambda _request: HttpResponseRedirect(location))(request).status_code

    def test_a_login_gate_is_refused(self) -> None:
        self.assertEqual(self.respond(f"{settings.LOGIN_URL}?next=/dashboard/x/"), 401)

    def test_a_plain_redirect_to_the_login_page_is_not_a_login_gate(self) -> None:
        """Only ``redirect_to_login`` adds ``next``."""
        self.assertEqual(self.respond(settings.LOGIN_URL), 302)

    def test_a_redirect_elsewhere_is_left_alone(self) -> None:
        self.assertEqual(self.respond("/dashboard/elsewhere/?next=/x/"), 302)

    def test_a_signed_in_viewer_sent_to_the_login_page_is_left_alone(self) -> None:
        """Staff-only views send a signed-in non-admin to sign in as someone else; that is not an ended session."""
        self.assertEqual(self.respond(f"{settings.LOGIN_URL}?next=/x/", signed_in=True), 302)

    def test_the_fetch_net_says_what_the_server_says(self) -> None:
        """shared/site-runtime.ts spells the message out for a raw fetch, which it cannot read without consuming."""
        runtime = Path(middleware.__file__).parent / "frontend" / "ts" / "shared" / "site-runtime.ts"
        self.assertIn(f'SESSION_ENDED_MESSAGE = "{SESSION_ENDED_MESSAGE}"', runtime.read_text(encoding="utf-8"))
