"""Tests for boolean environment-variable parsing in the settings modules.

``EMAIL_USE_TLS`` is the one that matters: it was parsed with a literal
``os.getenv(...) == "True"``, so any spelling but exactly ``True`` disabled
STARTTLS. That is the quiet-failure direction - SMTP credentials and every
outbound mail go over the wire in plaintext while ``.env`` says TLS is on - and
``UL_EMAIL_TLS`` is *also* declared in ``app.py`` as a pydantic ``bool``, which
happily accepts ``true``/``1``/``yes``. So the two readers of one variable
disagreed, and the disagreement resolved toward plaintext.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from hypothesis import given, strategies as st
from urbanlens.UrbanLens.settings._env import env_bool

_TRUTHY = ["1", "true", "True", "TRUE", "t", "yes", "Yes", "y", "on", "ON", " true "]
_FALSEY = ["0", "false", "False", "FALSE", "f", "no", "No", "n", "off", ""]


class EnvBoolTests(SimpleTestCase):
    def test_spellings_people_actually_write_are_all_truthy(self) -> None:
        for raw in _TRUTHY:
            with mock.patch.dict("os.environ", {"UL_TEST_FLAG": raw}), self.subTest(raw=raw):
                self.assertIs(env_bool("UL_TEST_FLAG", default=False), True)

    def test_falsey_spellings_are_all_falsey(self) -> None:
        for raw in _FALSEY:
            with mock.patch.dict("os.environ", {"UL_TEST_FLAG": raw}), self.subTest(raw=raw):
                self.assertIs(env_bool("UL_TEST_FLAG", default=True), False)

    def test_an_unset_variable_uses_the_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIs(env_bool("UL_TEST_FLAG", default=True), True)
            self.assertIs(env_bool("UL_TEST_FLAG", default=False), False)

    def test_an_unrecognised_value_falls_back_to_the_default(self) -> None:
        """Garbage must not silently mean False - for TLS that is the unsafe direction."""
        with mock.patch.dict("os.environ", {"UL_TEST_FLAG": "banana"}):
            self.assertIs(env_bool("UL_TEST_FLAG", default=True), True)

    @given(st.sampled_from(_TRUTHY), st.booleans())
    def test_a_truthy_value_wins_over_any_default(self, raw: str, default: bool) -> None:
        with mock.patch.dict("os.environ", {"UL_TEST_FLAG": raw}):
            self.assertIs(env_bool("UL_TEST_FLAG", default=default), True)


class EmailTlsSettingTests(SimpleTestCase):
    """The concrete setting the helper exists for."""

    def test_lowercase_true_enables_tls(self) -> None:
        with mock.patch.dict("os.environ", {"UL_EMAIL_TLS": "true"}):
            self.assertIs(env_bool("UL_EMAIL_TLS", default=True), True)

    def test_tls_defaults_on_when_unconfigured(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIs(env_bool("UL_EMAIL_TLS", default=True), True)

    def test_tls_can_still_be_turned_off_explicitly(self) -> None:
        with mock.patch.dict("os.environ", {"UL_EMAIL_TLS": "false"}):
            self.assertIs(env_bool("UL_EMAIL_TLS", default=True), False)
