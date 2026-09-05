"""Tests for services.device_scan.mac_address.normalize_mac_address.

Every device-scan write path funnels through this function so the same
physical device is never split across two ``ScannedDevice`` rows over a
casing or separator difference between two uploads - these tests are what
would catch a regression there.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from hypothesis import HealthCheck, given, settings, strategies as st
from urbanlens.dashboard.services.device_scan.mac_address import InvalidMacAddressError, normalize_mac_address

_HEX_OCTET = st.integers(min_value=0, max_value=255).map(lambda n: f"{n:02x}")
_OCTETS = st.lists(_HEX_OCTET, min_size=6, max_size=6)
_SEPARATORS = st.sampled_from([":", "-", ".", ""])

# This module's Django/GDAL-touching first draw occasionally takes seconds
# under this environment's cold start, which Hypothesis's generation-speed
# health check misreads as an expensive strategy - the strategies themselves
# (six small integers mapped to hex strings) are trivial.
_SUPPRESS_SLOW_DRAW = settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)


class NormalizeMacAddressTests(SimpleTestCase):
    """Fixed-example behavior for each accepted separator style and case."""

    def test_colon_separated_uppercase_is_returned_unchanged(self) -> None:
        self.assertEqual(normalize_mac_address("AA:BB:CC:DD:EE:FF"), "AA:BB:CC:DD:EE:FF")

    def test_colon_separated_lowercase_is_upper_cased(self) -> None:
        self.assertEqual(normalize_mac_address("aa:bb:cc:dd:ee:ff"), "AA:BB:CC:DD:EE:FF")

    def test_hyphen_separated_is_normalized(self) -> None:
        self.assertEqual(normalize_mac_address("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")

    def test_dot_separated_is_normalized(self) -> None:
        self.assertEqual(normalize_mac_address("aa.bb.cc.dd.ee.ff"), "AA:BB:CC:DD:EE:FF")

    def test_no_separator_is_normalized(self) -> None:
        self.assertEqual(normalize_mac_address("aabbccddeeff"), "AA:BB:CC:DD:EE:FF")

    def test_surrounding_whitespace_is_stripped(self) -> None:
        self.assertEqual(normalize_mac_address("  aa:bb:cc:dd:ee:ff  "), "AA:BB:CC:DD:EE:FF")

    def test_too_short_raises(self) -> None:
        with self.assertRaises(InvalidMacAddressError):
            normalize_mac_address("AA:BB:CC:DD:EE")

    def test_too_long_raises(self) -> None:
        with self.assertRaises(InvalidMacAddressError):
            normalize_mac_address("AA:BB:CC:DD:EE:FF:00")

    def test_non_hex_characters_raise(self) -> None:
        with self.assertRaises(InvalidMacAddressError):
            normalize_mac_address("GG:HH:CC:DD:EE:FF")

    def test_empty_string_raises(self) -> None:
        with self.assertRaises(InvalidMacAddressError):
            normalize_mac_address("")

    def test_mixed_separators_within_one_address_are_still_normalized(self) -> None:
        """Each octet pair's separator is checked independently, so a mixed style still parses."""
        self.assertEqual(normalize_mac_address("AA:BB-CC:DD-EE:FF"), "AA:BB:CC:DD:EE:FF")


class NormalizeMacAddressPropertyTests(SimpleTestCase):
    """Hypothesis coverage across every accepted separator/case combination."""

    @_SUPPRESS_SLOW_DRAW
    @given(octets=_OCTETS, separator=_SEPARATORS, uppercase=st.booleans())
    def test_normalize_is_separator_and_case_insensitive(
        self, octets: list[str], separator: str, uppercase: bool
    ) -> None:
        """Any valid separator style, in either case, resolves to the same canonical form."""
        raw = separator.join(octets)
        raw = raw.upper() if uppercase else raw.lower()
        expected = ":".join(octet.upper() for octet in octets)
        self.assertEqual(normalize_mac_address(raw), expected)

    @_SUPPRESS_SLOW_DRAW
    @given(octets=_OCTETS)
    def test_normalize_is_idempotent(self, octets: list[str]) -> None:
        """Normalizing an already-canonical address returns it unchanged."""
        canonical = normalize_mac_address(":".join(octets))
        self.assertEqual(normalize_mac_address(canonical), canonical)
