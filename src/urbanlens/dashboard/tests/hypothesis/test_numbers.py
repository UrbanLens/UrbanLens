"""`services.core.numbers` is the module that exists so a malformed field is not a 500.

It had no tests, and one input got through it: `int(float("inf"))` raises
`OverflowError`, which `safe_int` did not catch. That is reachable, not
theoretical - Python's `json.loads` accepts the bare literals `Infinity`,
`-Infinity` and `NaN`, and `controllers/detail_pins.py` and
`controllers/markup.py` both call `safe_int(body.get(...))` on a body parsed
that way.
"""

from __future__ import annotations

import json

import pytest

from hypothesis import given, strategies as st
from urbanlens.dashboard.services.core.numbers import clamp_int, safe_int, safe_int_or_none

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
