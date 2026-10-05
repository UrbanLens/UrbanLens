"""Properties of services.apis.locations.cid_validation, the one place a Google Maps CID is checked."""

from __future__ import annotations

from decimal import Decimal
import json

from hypothesis import assume, given, strategies as st
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.apis.locations.cid_validation import (
    CID_CONFLICTS_WITH_URL,
    CID_FLOAT_ROUNDED,
    CID_NOT_INTEGER,
    CID_OUT_OF_RANGE,
    FLOAT64_EXACT_LIMIT,
    MAX_CID,
    InvalidCidError,
    cid_stated_by,
    float_rounded,
    looks_float_rounded,
    parse_cid,
    reconcile_cid,
)

cids = st.integers(min_value=1, max_value=MAX_CID)
#: CIDs a float64 cannot hold: the only ones a float round trip can change.
wide_cids = st.integers(min_value=FLOAT64_EXACT_LIMIT + 1, max_value=MAX_CID)


def _place_url(cid: int) -> str:
    return f"https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x89c25a2b3c4d5e6f:0x{cid:x}"


class ParseCidTests(SimpleTestCase):
    @given(cids)
    def test_every_64_bit_cid_round_trips_as_int_string_and_decimal(self, cid: int) -> None:
        self.assertEqual(parse_cid(cid), cid)
        self.assertEqual(parse_cid(str(cid)), cid)
        self.assertEqual(parse_cid(Decimal(cid)), cid)
        self.assertEqual(parse_cid(json.loads(json.dumps(str(cid)))), cid)

    @given(st.floats(allow_nan=True, allow_infinity=True))
    def test_a_float_is_never_a_cid(self, value: float) -> None:
        with self.assertRaises(InvalidCidError) as caught:
            parse_cid(value)
        self.assertEqual(caught.exception.code, CID_NOT_INTEGER)

    def test_malformed_values_are_not_integers(self) -> None:
        for value in (
            True,
            False,
            "",
            " ",
            "1e19",
            "+5",
            "-5",
            "0x1f",
            "1.0",
            "１２",
            "123456789012345678901",
            Decimal("1.5"),
            Decimal("NaN"),
            None,
            [],
            {},
        ):
            with self.subTest(value=value), self.assertRaises(InvalidCidError) as caught:
                parse_cid(value)
            self.assertEqual(caught.exception.code, CID_NOT_INTEGER)

    def test_zero_negative_and_too_wide_are_out_of_range(self) -> None:
        for value in (0, -1, "0", MAX_CID + 1, "18446744073709551616", Decimal(-3)):
            with self.subTest(value=value), self.assertRaises(InvalidCidError) as caught:
                parse_cid(value)
            self.assertEqual(caught.exception.code, CID_OUT_OF_RANGE)


class LooksFloatRoundedTests(SimpleTestCase):
    @given(wide_cids)
    def test_whatever_a_float_round_trip_produces_looks_float_rounded(self, cid: int) -> None:
        assume(float_rounded(cid) > FLOAT64_EXACT_LIMIT)  # 2**53 + 1 rounds to 2**53, which a float64 holds exactly.
        self.assertTrue(looks_float_rounded(float_rounded(cid)))

    @given(st.integers(min_value=1, max_value=FLOAT64_EXACT_LIMIT))
    def test_nothing_a_float64_holds_exactly_looks_float_rounded(self, cid: int) -> None:
        self.assertFalse(looks_float_rounded(cid))

    def test_a_cid_from_reDatas_staging_rows(self) -> None:
        self.assertTrue(looks_float_rounded(6827058720975719000))
        self.assertFalse(looks_float_rounded(11148154607565040795))


class CidStatedByTests(SimpleTestCase):
    @given(cids)
    def test_every_shape_of_url_states_its_cid(self, cid: int) -> None:
        self.assertEqual(cid_stated_by(_place_url(cid)), cid)
        self.assertEqual(cid_stated_by(f"https://maps.google.com/?cid={cid}"), cid)
        self.assertEqual(cid_stated_by(f"https://maps.google.com/maps?hl=en&cid={cid}&q=x"), cid)
        self.assertEqual(cid_stated_by(f"0x89c25a2b3c4d5e6f:0x{cid:x}"), cid)

    def test_a_url_naming_no_cid_states_none(self) -> None:
        for url in (
            "",
            "https://www.google.com/maps/place/X/@40.1,-74.2,17z",
            "https://www.google.com/maps/place/X/data=!4m2!3m1!1s0x89c25a2b3c4d5e6f:0x0",
            "https://maps.google.com/?cid=0",
            "https://maps.google.com/?cid=123456789012345678901",
        ):
            with self.subTest(url=url):
                self.assertIsNone(cid_stated_by(url))


class ReconcileCidTests(SimpleTestCase):
    @given(cids)
    def test_a_claim_alone_is_kept_unless_it_looks_float_rounded(self, cid: int) -> None:
        if looks_float_rounded(cid):
            with self.assertRaises(InvalidCidError) as caught:
                reconcile_cid(cid)
            self.assertEqual(caught.exception.code, CID_FLOAT_ROUNDED)
        else:
            self.assertEqual(reconcile_cid(cid), cid)
            self.assertEqual(reconcile_cid(str(cid)), cid)

    @given(wide_cids)
    def test_a_float_rounded_claim_with_its_url_becomes_the_urls_cid(self, cid: int) -> None:
        # Within about a thousand of 2**64 the rounded claim is itself past 2**64 - 1, and refused as such.
        assume(float_rounded(cid) <= MAX_CID)
        self.assertEqual(reconcile_cid(float_rounded(cid), url=_place_url(cid)), cid)
        self.assertEqual(reconcile_cid(str(float_rounded(cid)), url=_place_url(cid)), cid)

    @given(cids)
    def test_a_url_alone_or_agreeing_names_its_cid(self, cid: int) -> None:
        self.assertEqual(reconcile_cid(None, url=_place_url(cid)), cid)
        self.assertEqual(reconcile_cid(cid, url=_place_url(cid)), cid)
        self.assertEqual(reconcile_cid("", url=_place_url(cid)), cid)

    @given(cids, cids)
    def test_a_claim_naming_another_place_than_its_url_is_refused(self, claimed: int, stated: int) -> None:
        assume(claimed not in {stated, float_rounded(stated)})
        with self.assertRaises(InvalidCidError) as caught:
            reconcile_cid(claimed, url=_place_url(stated))
        self.assertEqual(caught.exception.code, CID_CONFLICTS_WITH_URL)

    def test_a_rounded_claim_past_2_to_the_64_is_out_of_range_even_with_its_url(self) -> None:
        with self.assertRaises(InvalidCidError) as caught:
            reconcile_cid(float_rounded(MAX_CID), url=_place_url(MAX_CID))
        self.assertEqual(caught.exception.code, CID_OUT_OF_RANGE)

    def test_no_claim_and_no_url_names_nothing(self) -> None:
        self.assertIsNone(reconcile_cid(None))
        self.assertIsNone(reconcile_cid(None, url="https://www.google.com/maps/place/X/@40.1,-74.2,17z"))
