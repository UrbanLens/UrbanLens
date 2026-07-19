"""Tests for the shared photo-reposition payload parser and its endpoints.

The pin, wiki, and safety gallery reposition endpoints all accepted a dragged
marker's ``{"latitude", "longitude"}`` JSON with the same subtle hole: they
caught ``ValueError``, but ``Decimal("abc")`` raises
``decimal.InvalidOperation`` (an ``ArithmeticError``), so garbage input
500'd instead of 400ing - and ``Decimal("nan")`` parsed fine (Postgres
``numeric`` stores NaN), so nothing rejected non-finite coordinates.
``parse_reposition_payload`` centralizes the validation; these tests cover
the parser exhaustively plus one endpoint-level regression per bug class.
"""

from __future__ import annotations

from decimal import Decimal
import json

from django.contrib.auth.models import User
from django.urls import reverse
from hypothesis import given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.images import parse_reposition_payload


def _body(latitude: object, longitude: object) -> bytes:
    return json.dumps({"latitude": latitude, "longitude": longitude}).encode()


class ParseRepositionPayloadTests(SimpleTestCase):
    """The shared parser: valid coordinates round-trip, everything else raises ValueError."""

    @given(
        st.decimals(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False, places=6),
        st.decimals(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False, places=6),
    )
    @settings(max_examples=30, deadline=None)
    def test_valid_coordinates_round_trip(self, latitude: Decimal, longitude: Decimal) -> None:
        parsed_lat, parsed_lng = parse_reposition_payload(_body(str(latitude), str(longitude)))
        self.assertEqual(parsed_lat, latitude)
        self.assertEqual(parsed_lng, longitude)

    def test_numeric_json_values_are_accepted(self) -> None:
        parsed_lat, parsed_lng = parse_reposition_payload(_body(40.5, -74.25))
        self.assertEqual(parsed_lat, Decimal("40.5"))
        self.assertEqual(parsed_lng, Decimal("-74.25"))

    def test_non_numeric_string_raises_value_error_not_invalid_operation(self) -> None:
        """The original bug: Decimal("abc") raises InvalidOperation, which the
        old per-endpoint handlers didn't catch."""
        with self.assertRaises(ValueError):
            parse_reposition_payload(_body("abc", "-74.0"))

    def test_nan_and_infinity_are_rejected(self) -> None:
        for value in ("nan", "NaN", "inf", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_reposition_payload(_body(value, "-74.0"))

    def test_out_of_range_coordinates_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_reposition_payload(_body("90.000001", "0"))
        with self.assertRaises(ValueError):
            parse_reposition_payload(_body("0", "-180.5"))

    def test_boundary_coordinates_are_accepted(self) -> None:
        self.assertEqual(parse_reposition_payload(_body("90", "-180")), (Decimal("90"), Decimal("-180")))

    def test_missing_keys_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_reposition_payload(json.dumps({"latitude": "40.0"}).encode())

    def test_non_object_payloads_are_rejected(self) -> None:
        """A JSON array/scalar body used to raise an uncaught TypeError."""
        for body in (b"[]", b'"40.0"', b"null", b"not json at all"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_reposition_payload(body)

    def test_null_coordinate_values_are_rejected(self) -> None:
        # str(None) == "None", which Decimal rejects via InvalidOperation.
        with self.assertRaises(ValueError):
            parse_reposition_payload(_body(None, None))


class PinImageRepositionEndpointTests(TestCase):
    """Endpoint-level regression: bad payloads must 400, never 500."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.pin = baker.make(Pin, profile=self.user.profile)
        self.image = baker.make(Image, pin=self.pin, profile=self.user.profile)
        self.client.force_login(self.user)
        self.url = reverse("pin.gallery.image", kwargs={"pin_slug": self.pin.slug, "image_id": self.image.pk})

    def _post(self, payload: dict) -> object:
        return self.client.post(self.url, data=json.dumps(payload), content_type="application/json")

    def test_valid_reposition_saves_and_echoes_coordinates(self) -> None:
        response = self._post({"latitude": "40.123456", "longitude": "-74.654321"})
        self.assertEqual(response.status_code, 200)
        self.image.refresh_from_db()
        self.assertEqual(self.image.latitude, Decimal("40.123456"))
        self.assertEqual(self.image.longitude, Decimal("-74.654321"))

    def test_garbage_coordinate_returns_400_not_500(self) -> None:
        response = self._post({"latitude": "abc", "longitude": "-74.0"})
        self.assertEqual(response.status_code, 400)

    def test_nan_coordinate_returns_400_and_stores_nothing(self) -> None:
        response = self._post({"latitude": "nan", "longitude": "-74.0"})
        self.assertEqual(response.status_code, 400)
        self.image.refresh_from_db()
        self.assertIsNone(self.image.latitude)

    def test_other_users_image_is_404(self) -> None:
        other = baker.make(User)
        self.client.force_login(other)
        response = self._post({"latitude": "40.0", "longitude": "-74.0"})
        self.assertEqual(response.status_code, 404)
