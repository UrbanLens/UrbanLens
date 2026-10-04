"""A parameter value no parser expects is refused or ignored, never a 500 or a stored row (P283).

``float()`` reads ``inf`` and ``nan``, ``Decimal()`` reads both and ``1e999``, ``int()`` reads a number past a 64-bit
column, and a ``UUIDField`` lookup raises ``ValidationError`` on anything that is not a UUID.
"""

from __future__ import annotations

from decimal import Decimal
import json
from uuid import UUID

from django.contrib.auth.models import User
from django.core.exceptions import BadRequest
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from hypothesis import given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_list.model import PinList
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.property_owner.model import PinOwner, PinPropertySale
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.services.billing.pricing import STRIPE_MAXIMUM_CHARGE_CENTS, typed_dollars_to_cents
from urbanlens.dashboard.services.core.numbers import DB_BIGINT_MAX, typed_decimal_for_column
from urbanlens.dashboard.services.core.pagination import offset_window
from urbanlens.dashboard.services.core.request_body import json_body, list_field, text_field, text_type_error
from urbanlens.dashboard.services.core.uuids import uuid_or_none, valid_uuids
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile
from urbanlens.dashboard.services.trips.trip_crud import create_trip

_OFF_THE_GLOBE = ("inf", "-inf", "nan", "1e999", "95", "-90.0001")


class _SignedIn(TestCase):
    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile, _ = Profile.objects.get_or_create(user=self.user)
        self.client.force_login(self.user)


class AddingAPinTests(_SignedIn):
    def test_a_latitude_off_the_globe_places_nothing(self) -> None:
        for latitude in _OFF_THE_GLOBE:
            with self.subTest(latitude=latitude):
                response = self.client.post(
                    reverse("pin.add"), {"name": "Mill", "latitude": latitude, "longitude": "-73.5"}
                )

                self.assertEqual(response.status_code, 400)
                self.assertFalse(Pin.objects.filter(profile=self.profile).exists())
                self.assertFalse(Location.objects.exists())

    def test_a_label_id_that_is_not_a_number_is_dropped_and_the_pin_is_placed(self) -> None:
        label = baker.make(Label, profile=self.profile, name="Mills")

        response = self.client.post(
            reverse("pin.add"),
            {"name": "Mill", "latitude": "42.5", "longitude": "-73.5", "label_ids": ["inf", str(label.pk)]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(Pin.objects.get(profile=self.profile).labels.all()), [label])


class QuickEditingAPinTests(_SignedIn):
    def setUp(self) -> None:
        super().setUp()
        self.pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin

    def test_a_latitude_off_the_globe_moves_nothing(self) -> None:
        for latitude in _OFF_THE_GLOBE:
            with self.subTest(latitude=latitude):
                response = self.client.post(
                    reverse("pin.quick_edit", args=[self.pin.slug]),
                    {"name": "Renamed", "latitude": latitude, "longitude": "-73.5"},
                )

                self.assertEqual(response.status_code, 400)
                self.pin.refresh_from_db()
                self.assertEqual((self.pin.location.latitude, self.pin.name), (Decimal("42.5"), "Mill"))

    def test_a_move_on_the_globe_still_moves(self) -> None:
        """Anti-vacuity for the test above."""
        response = self.client.post(
            reverse("pin.quick_edit", args=[self.pin.slug]), {"latitude": "43.5", "longitude": "-73.5"}
        )

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location.latitude, Decimal("43.5"))


class TextPastItsColumnTests(_SignedIn):
    """A name longer than its column was a ``DataError`` on every route below but the ones with a form."""

    def setUp(self) -> None:
        super().setUp()
        self.pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin

    def test_a_pin_name_past_its_column_is_refused(self) -> None:
        response = self.client.post(reverse("pin.edit", args=[self.pin.slug]), {"name": "n" * 256})

        self.assertEqual(response.status_code, 400)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Mill")

    def test_an_owner_name_past_its_column_records_no_owner(self) -> None:
        response = self.client.post(reverse("pin.ownership", args=[self.pin.slug]), {"name": "n" * 201})

        self.assertIn("200 characters", response.headers.get("HX-Trigger", ""))
        self.assertFalse(PinOwner.objects.exists())

    def test_a_trip_name_past_its_column_and_a_date_that_is_not_one_are_refused(self) -> None:
        for fields in (
            {"name": "n" * 256},
            {"name": "Walk", "start_date": "inf"},
            {"name": "Walk", "end_date": "1e999"},
        ):
            with self.subTest(fields=list(fields)):
                response = self.client.post(reverse("trips.create"), fields)

                self.assertEqual(response.status_code, 400)
                self.assertFalse(Trip.objects.exists())

    def test_a_trip_with_a_name_and_dates_is_created(self) -> None:
        """Anti-vacuity for the test above."""
        self.client.post(
            reverse("trips.create"), {"name": "Walk", "start_date": "2026-05-01", "end_date": "2026-05-02"}
        )

        self.assertEqual(Trip.objects.get().start_date.isoformat(), "2026-05-01")


class MovingAPinOntoAnotherTests(_SignedIn):
    def test_a_quick_edit_onto_a_place_already_pinned_is_refused(self) -> None:
        create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5)
        moving = create_pin_for_profile(self.profile, name="Kiln", latitude=43.5, longitude=-73.5).pin

        response = self.client.post(
            reverse("pin.quick_edit", args=[moving.slug]), {"latitude": "42.5", "longitude": "-73.5"}
        )

        self.assertEqual(response.status_code, 400)
        moving.refresh_from_db()
        self.assertEqual(moving.location.latitude, Decimal("43.5"))


class JsonFieldTypeTests(_SignedIn):
    def test_a_json_field_that_is_not_text_is_no_text(self) -> None:
        for value in (1.5, True, None, [], {}, ["x"]):
            with self.subTest(value=value):
                response = self.client.post(
                    reverse("lists.create"), data=json.dumps({"name": value}), content_type="application/json"
                )

                self.assertEqual(response.status_code, 400)
        self.assertFalse(PinList.objects.exists())

    def test_an_update_refuses_a_text_field_of_another_type_rather_than_clearing_it(self) -> None:
        pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin
        Pin.objects.filter(pk=pin.pk).update(description="Kept")
        trip, _ = create_trip(self.profile, name="Walk", description="Kept")
        routes = (reverse("pin.edit", args=[pin.slug]), reverse("trips.edit", args=[trip.slug]))
        for url in routes:
            for value in (5, True, [], {}):
                with self.subTest(url=url, value=value):
                    response = self.client.post(
                        url, data=json.dumps({"description": value}), content_type="application/json"
                    )

                    self.assertEqual(response.status_code, 400)
        pin.refresh_from_db()
        trip.refresh_from_db()
        self.assertEqual((pin.description, trip.description), ("Kept", "Kept"))

    def test_an_update_still_clears_a_text_field_posted_as_null(self) -> None:
        """Anti-vacuity: null is how a JSON client clears one."""
        trip, _ = create_trip(self.profile, name="Walk", description="Gone")

        self.client.post(
            reverse("trips.edit", args=[trip.slug]),
            data=json.dumps({"description": None}),
            content_type="application/json",
        )

        trip.refresh_from_db()
        self.assertIsNone(trip.description)

    def test_the_accessors(self) -> None:
        fields = {"text": "  a  ", "number": 1.5, "items": [1], "map": {}}

        self.assertEqual([text_field(fields, key) for key in ("text", "number", "items", "absent")], ["a", "", "", ""])
        self.assertEqual([list_field(fields, key) for key in ("items", "text", "map", "absent")], [[1], [], [], []])
        self.assertEqual(text_type_error({"a": "x", "b": None, "c": 1}, "a", "b", "absent"), None)
        self.assertEqual(text_type_error({"a": "x", "c": 1}, "a", "c"), "c must be text.")

    def test_a_form_posted_where_json_is_read_is_a_bad_request(self) -> None:
        pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin
        image = baker.make(Image, profile=self.profile, pin=pin)

        response = self.client.post(reverse("pin.gallery.image", args=[pin.slug, image.pk]), {"latitude": "1"})

        self.assertEqual(response.status_code, 400)
        with self.assertRaises(BadRequest):
            json_body(RequestFactory().post("/x/", {"a": "1"}))


class RecordingASaleTests(_SignedIn):
    def test_a_price_that_is_not_a_finite_number_the_column_holds_records_nothing(self) -> None:
        pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin
        for price in ("nan", "inf", "-inf", "1e999", "1e10", "abc"):
            with self.subTest(price=price):
                response = self.client.post(reverse("pin.sales", args=[pin.slug]), {"sale_price": price})

                self.assertIn("Invalid sale price.", response.headers.get("HX-Trigger", ""))
                self.assertFalse(PinPropertySale.objects.exists())

    def test_a_price_the_column_holds_is_recorded(self) -> None:
        pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin

        self.client.post(reverse("pin.sales", args=[pin.slug]), {"sale_price": "9999999999.99"})

        self.assertEqual(PinPropertySale.objects.get().sale_price, Decimal("9999999999.99"))


class LookupsByIdentifierTests(_SignedIn):
    def test_a_label_id_past_a_number_names_no_label(self) -> None:
        pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin
        for label_id in ("inf", "-1e999", "99999999999999999999999"):
            with self.subTest(label_id=label_id):
                response = self.client.post(
                    reverse("label.pin", kwargs={"label_kind": "tags", "pin_slug": pin.slug}),
                    {"action": "add", "label_id": label_id},
                )

                self.assertEqual(response.status_code, 404)

    def test_a_uuid_parameter_that_is_not_one_names_nothing(self) -> None:
        pin = create_pin_for_profile(self.profile, name="Mill", latitude=42.5, longitude=-73.5).pin

        self.assertEqual(self.client.get(reverse("pin.parent_search"), {"q": "Mi", "exclude": "abc"}).status_code, 200)
        self.assertEqual(self.client.get(reverse("pin.bulk_edit.label_options"), {"uuids": "abc"}).status_code, 200)
        self.assertEqual(
            self.client.get(reverse("pin.share.dialog", args=[pin.slug]), {"children": "abc,"}).status_code, 200
        )

    def test_an_offset_past_a_64_bit_integer_is_an_empty_page(self) -> None:
        response = self.client.get(reverse("vault.photos.items"), {"offset": "9" * 30, "limit": "9" * 30})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])


class TypedDollarsTests(SimpleTestCase):
    def test_what_stripe_could_not_charge_is_none(self) -> None:
        for raw in ("nan", "inf", "-inf", "1e999999999", "-0.01", "1000000", "abc", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(typed_dollars_to_cents(raw))

    def test_amounts_stripe_charges_are_cents(self) -> None:
        self.assertEqual(typed_dollars_to_cents(" 0.5 "), 50)
        self.assertEqual(typed_dollars_to_cents("0"), 0)
        self.assertEqual(typed_dollars_to_cents("999999.99"), STRIPE_MAXIMUM_CHARGE_CENTS)


class TypedDecimalTests(SimpleTestCase):
    def test_what_the_column_cannot_hold_is_none(self) -> None:
        for raw in ("nan", "-nan", "snan", "inf", "1e999", "1e10", "abc", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(typed_decimal_for_column(raw, PinPropertySale, "sale_price"))

    def test_a_number_the_column_holds_is_rounded_as_it_would_store_it(self) -> None:
        self.assertEqual(typed_decimal_for_column(" 12.345 ", PinPropertySale, "sale_price"), Decimal("12.35"))

    def test_a_field_that_is_not_a_decimal_is_a_programming_error(self) -> None:
        with self.assertRaises(TypeError):
            typed_decimal_for_column("1", PinPropertySale, "notes")


class OffsetWindowTests(SimpleTestCase):
    @given(st.dictionaries(st.sampled_from(["offset", "limit"]), st.text(max_size=40) | st.integers().map(str)))
    def test_the_window_is_one_a_query_can_be_given(self, query: dict[str, str]) -> None:
        offset, limit = offset_window(query, default_limit=20, max_limit=100)

        self.assertTrue(0 <= offset <= DB_BIGINT_MAX)
        self.assertTrue(1 <= limit <= 100)

    def test_a_window_asked_for_is_the_window_given(self) -> None:
        self.assertEqual(offset_window({"offset": "40", "limit": "10"}, default_limit=20, max_limit=100), (40, 10))
        self.assertEqual(offset_window({}, default_limit=20, max_limit=100), (0, 20))


class UuidParsingTests(SimpleTestCase):
    def test_only_uuids_are_kept(self) -> None:
        good = UUID(int=7)

        self.assertEqual(valid_uuids(["abc", str(good), "", None, 3, good, "inf"]), [good, good])
        self.assertIsNone(uuid_or_none("x\x00y"))
