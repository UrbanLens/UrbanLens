"""A NUL character in a request is a 400, before a view hands it to Postgres, which refuses it in text (P283).

Twenty-five routes answered one with a 500: every search, and every write of a text field outside a Django form.
"""

from __future__ import annotations

import io
import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from model_bakery import baker
from rest_framework.exceptions import ParseError

from hypothesis import find, given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.middleware import NulCharacterRefusalMiddleware
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.core.request_body import JSONParser, MalformedBodyError, decode_json


def _refusal(request, view_kwargs: dict | None = None) -> HttpResponse | None:
    middleware = NulCharacterRefusalMiddleware(lambda _request: HttpResponse())
    return middleware.process_view(request, lambda _request: HttpResponse(), (), view_kwargs or {})


class ThroughARouteTests(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.client.force_login(self.user)

    def test_a_form_field_holding_a_nul_is_refused_and_nothing_is_written(self) -> None:
        response = self.client.post(reverse("lists.create"), {"name": "x\x00y"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinList.objects.filter(profile=self.profile).exists())

    def test_a_json_body_holding_a_nul_is_refused_and_nothing_is_written(self) -> None:
        response = self.client.post(
            reverse("lists.create"), data=json.dumps({"name": "x\x00y"}), content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinList.objects.filter(profile=self.profile).exists())

    def test_a_query_parameter_holding_a_nul_is_refused(self) -> None:
        response = self.client.get(reverse("messages.recipients"), {"q": "x\x00y"})

        self.assertEqual(response.status_code, 400)

    def test_the_same_requests_without_the_nul_are_answered(self) -> None:
        """Anti-vacuity: the routes above refuse the NUL, not the request."""
        self.client.post(reverse("lists.create"), {"name": "xy"})
        self.client.post(reverse("lists.create"), data=json.dumps({"name": "yz"}), content_type="application/json")

        self.assertEqual(set(PinList.objects.filter(profile=self.profile).values_list("name", flat=True)), {"xy", "yz"})
        self.assertEqual(self.client.get(reverse("messages.recipients"), {"q": "xy"}).status_code, 200)


class TheMiddlewareTests(SimpleTestCase):
    def test_a_nul_in_a_field_name_is_refused(self) -> None:
        self.assertEqual(_refusal(RequestFactory().post("/x/", {"na\x00me": "x"})).status_code, 400)

    def test_a_nul_in_any_value_of_a_repeated_field_is_refused(self) -> None:
        self.assertEqual(_refusal(RequestFactory().get("/x/", {"id": ["1", "2\x00"]})).status_code, 400)

    def test_a_nul_in_a_url_argument_is_refused(self) -> None:
        self.assertEqual(_refusal(RequestFactory().get("/x/"), {"slug": "x\x00y"}).status_code, 400)

    def test_the_refusal_is_plain_text_a_toast_can_show(self) -> None:
        response = _refusal(RequestFactory().get("/x/", {"q": "\x00"}))

        self.assertTrue(response["Content-Type"].startswith("text/plain"))
        self.assertIn("NUL", response.content.decode())

    def test_an_uploaded_files_bytes_are_not_read(self) -> None:
        upload = SimpleUploadedFile("photo.jpg", b"\xff\xd8\x00\x00\x00\xff\xd9", content_type="image/jpeg")
        request = RequestFactory().post("/x/", {"file": upload, "name": "fine"})

        self.assertIsNone(_refusal(request))

    def test_a_csrf_exempt_views_multipart_body_is_left_for_it_to_read(self) -> None:
        """Reading the form would consume the body, and the view could no longer read ``request.body``."""
        request = RequestFactory().post("/x/", {"name": "x\x00y"})
        middleware = NulCharacterRefusalMiddleware(lambda _request: HttpResponse())

        self.assertIsNone(middleware.process_view(request, csrf_exempt(lambda _request: HttpResponse()), (), {}))
        self.assertIn(b"x\x00y", request.body)

    def test_a_csrf_exempt_views_urlencoded_form_is_still_read(self) -> None:
        """Django keeps an urlencoded body after parsing it, so reading the form costs the view nothing."""
        request = RequestFactory().post("/x/", "name=x%00y", content_type="application/x-www-form-urlencoded")
        middleware = NulCharacterRefusalMiddleware(lambda _request: HttpResponse())

        response = middleware.process_view(request, csrf_exempt(lambda _request: HttpResponse()), (), {})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(request.body, b"name=x%00y")

    def test_a_request_without_a_nul_passes(self) -> None:
        self.assertIsNone(_refusal(RequestFactory().post("/x/?q=a", {"name": "b"}), {"slug": "c", "pk": 3}))


_JSON_TEXT = st.text(alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)), max_size=8)
_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | _JSON_TEXT,
    lambda children: st.lists(children, max_size=4) | st.dictionaries(_JSON_TEXT, children, max_size=4),
    max_leaves=12,
)


def _holds_nul(value: object) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(map(_holds_nul, value))
    if isinstance(value, dict):
        return any(_holds_nul(key) or _holds_nul(item) for key, item in value.items())
    return False


class DecodedJsonTests(SimpleTestCase):
    @given(_JSON_VALUES)
    def test_json_is_refused_exactly_when_a_string_in_it_holds_a_nul(self, value: object) -> None:
        raw = json.dumps(value)

        if _holds_nul(value):
            with self.assertRaises(MalformedBodyError):
                decode_json(raw)
            with self.assertRaises(ParseError):
                JSONParser().parse(io.BytesIO(raw.encode()))
        else:
            self.assertEqual(decode_json(raw), value)
            self.assertEqual(JSONParser().parse(io.BytesIO(raw.encode())), value)

    def test_an_escaped_backslash_before_u0000_is_not_a_nul(self) -> None:
        self.assertEqual(decode_json(r'{"a": "\\u0000"}'), {"a": "\\u0000"})

    def test_the_strategy_draws_a_nul_in_a_key_and_in_a_nested_value(self) -> None:
        """Anti-vacuity for the property above."""
        in_a_key = find(_JSON_VALUES, lambda value: isinstance(value, dict) and any("\x00" in key for key in value))
        nested = find(
            _JSON_VALUES,
            lambda value: (
                isinstance(value, list) and any(isinstance(item, list | dict) and _holds_nul(item) for item in value)
            ),
        )

        self.assertTrue(_holds_nul(in_a_key))
        self.assertTrue(_holds_nul(nested))
