"""A view reads a posted body as an object, whatever was actually sent (P29: a JSON array was a 500 on six routes; it is a 400)."""

from __future__ import annotations

import json

from django.core.exceptions import BadRequest
from django.test import RequestFactory

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.core.request_body import posted_fields, posted_json_object


def _json(body: object):
    return RequestFactory().post("/x/", data=json.dumps(body), content_type="application/json")


class PostedJsonObjectTests(SimpleTestCase):
    def test_an_object_is_returned(self) -> None:
        self.assertEqual(posted_json_object(_json({"a": 1})), {"a": 1})

    def test_an_empty_body_is_empty(self) -> None:
        self.assertEqual(
            posted_json_object(RequestFactory().post("/x/", data=b"", content_type="application/json")), {}
        )

    def test_anything_that_is_not_an_object_is_a_bad_request(self) -> None:
        for body in ([1, 2], "text", 3, None):
            with self.subTest(body=body), self.assertRaises(BadRequest):
                posted_json_object(_json(body))
        for raw in (b"{not json", b"[" * 100_000):
            request = RequestFactory().post("/x/", data=raw, content_type="application/json")
            with self.subTest(raw=raw[:10]), self.assertRaises(BadRequest):
                posted_json_object(request)

    def test_a_view_catching_value_error_answers_it(self) -> None:
        with self.assertRaises(ValueError):
            posted_json_object(_json([1]))

    def test_a_form_post_is_not_read_as_json(self) -> None:
        request = RequestFactory().post("/x/", data={"a": "1"})
        request.POST  # noqa: B018 - a view that read the form first must not make the body unreadable
        self.assertEqual(posted_json_object(request), {})


class PostedFieldsTests(SimpleTestCase):
    def test_a_form_post_gives_one_value_per_field(self) -> None:
        request = RequestFactory().post("/x/", data={"label": "Door", "color": "#fff"})
        self.assertEqual(posted_fields(request), {"label": "Door", "color": "#fff"})

    def test_a_json_post_gives_its_object(self) -> None:
        self.assertEqual(posted_fields(_json({"label": "Door"})), {"label": "Door"})
        with self.assertRaises(BadRequest):
            posted_fields(_json(["Door"]))
