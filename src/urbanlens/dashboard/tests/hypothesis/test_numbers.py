"""`services.core.numbers` is the module that exists so a malformed field is not a 500."""

from __future__ import annotations

import json

import pytest

from hypothesis import given, strategies as st
from urbanlens.dashboard.services.core.numbers import (
    LATITUDE_BOUND,
    LONGITUDE_BOUND,
    clamp_int,
    coordinate_or_none,
    safe_int,
    safe_int_or_none,
)

#: Every value that reaches these helpers from a request and is not an int.
NOT_AN_INT = ["", "abc", "12abc", "5.0", "0x3", None, [], {}, object(), b"nope"]


class TestSafeInt:
    def test_parses_what_it_can(self) -> None:
        assert safe_int("42") == 42
        assert safe_int(42) == 42
        assert safe_int(b"42") == 42
        assert safe_int(3.9) == 3
        assert safe_int(" 7 ") == 7

    def test_falls_back_for_everything_else(self) -> None:
        for value in NOT_AN_INT:
            assert safe_int(value, 99) == 99, f"{value!r} should have fallen back"

    def test_bool_is_not_treated_as_its_int_value(self) -> None:
        assert safe_int(True, 99) == 99
        assert safe_int(False, 99) == 99

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
    def test_a_json_body_carrying_a_float_literal_falls_back(self, literal: str) -> None:
        """`int(float("inf"))` raises OverflowError, which is neither TypeError nor ValueError."""
        body = json.loads(f'{{"opacity": {literal}}}')
        assert safe_int(body["opacity"], 80) == 80

    @given(st.integers())
    def test_an_int_round_trips_through_its_own_string(self, value: int) -> None:
        assert safe_int(str(value), -1) == value


class TestSafeIntOrNone:
    def test_parses_what_it_can(self) -> None:
        assert safe_int_or_none("42") == 42
        assert safe_int_or_none(42) == 42
        assert safe_int_or_none(0) == 0, "0 is a value, not an absence"

    def test_returns_none_for_everything_else(self) -> None:
        for value in NOT_AN_INT:
            assert safe_int_or_none(value) is None, f"{value!r} should have been None"

    def test_bool_is_not_treated_as_its_int_value(self) -> None:
        assert safe_int_or_none(True) is None
        assert safe_int_or_none(False) is None

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
    def test_a_json_float_literal_is_none(self, literal: str) -> None:
        body = json.loads(f'{{"id": {literal}}}')
        assert safe_int_or_none(body["id"]) is None


class TestClampInt:
    def test_constrains_to_the_range(self) -> None:
        assert clamp_int("500", low=0, high=100, default=50) == 100
        assert clamp_int("-500", low=0, high=100, default=50) == 0
        assert clamp_int("42", low=0, high=100, default=50) == 42

    def test_an_out_of_range_default_is_clamped_too(self) -> None:
        assert clamp_int("abc", low=0, high=100, default=999) == 100

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN"])
    def test_a_json_float_literal_reaches_the_default(self, literal: str) -> None:
        body = json.loads(f'{{"opacity": {literal}}}')
        assert clamp_int(body["opacity"], low=0, high=100, default=80) == 80


class TestCoordinateOrNone:
    def test_parses_a_coordinate_in_range(self) -> None:
        assert coordinate_or_none("42.5", bound=LATITUDE_BOUND) == 42.5
        assert coordinate_or_none(-180, bound=LONGITUDE_BOUND) == -180.0
        assert coordinate_or_none(0, bound=LATITUDE_BOUND) == 0.0

    def test_refuses_what_is_not_a_finite_coordinate_in_range(self) -> None:
        for value in ["", "north", None, [42.0], {"lat": 1}, True, float("nan"), float("inf"), "1e999", 90.5]:
            assert coordinate_or_none(value, bound=LATITUDE_BOUND) is None, value

    @given(st.floats(allow_nan=True, allow_infinity=True))
    def test_anything_returned_is_finite_and_in_range(self, value: float) -> None:
        parsed = coordinate_or_none(value, bound=LONGITUDE_BOUND)
        assert parsed is None or -LONGITUDE_BOUND <= parsed <= LONGITUDE_BOUND
