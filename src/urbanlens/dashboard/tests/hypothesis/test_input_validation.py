"""An input that cannot return data is refused before the limiter, the budget or the network."""

from __future__ import annotations

from datetime import date
import io
import logging
import math
import re
from unittest import mock

from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.core import input_validation
from urbanlens.dashboard.services.core.gateway import GatewayRequestError, is_source_outage
from urbanlens.dashboard.services.core.input_validation import (
    ImpossibleInputError,
    InputRejection,
    check_request_parameters,
    coordinate_problem,
    require_coordinates,
    require_date_within,
    require_format,
    require_in_range,
    require_jpeg_size,
    require_query,
)
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError, _RateLimitedSession, check_rate_limit

SERVICE = "test_input_validation"


class ImpossibleInputErrorTests(TestCase):
    def test_is_a_gateway_error_that_is_never_an_outage(self) -> None:
        """Callers degrade on GatewayRequestError; only an outage may stay uncached."""
        error = ImpossibleInputError(SERVICE, InputRejection.EMPTY_QUERY, "the query is blank")

        self.assertIsInstance(error, GatewayRequestError)
        self.assertNotIsInstance(error, RequestCancelledError)
        self.assertFalse(error.is_outage)
        self.assertFalse(is_source_outage(error))
        self.assertIs(error.reason, InputRejection.EMPTY_QUERY)
        self.assertEqual(error.service, SERVICE)


class RequireCoordinatesTests(TestCase):
    def test_a_real_place_passes_through_as_floats(self) -> None:
        self.assertEqual(require_coordinates(SERVICE, "40.7", -74), (40.7, -74.0))

    def test_the_poles_and_the_antimeridian_are_real_places(self) -> None:
        self.assertEqual(require_coordinates(SERVICE, 90, 180), (90.0, 180.0))

    def test_one_zero_component_is_a_real_place(self) -> None:
        self.assertEqual(require_coordinates(SERVICE, 0, 32.5), (0.0, 32.5))

    def test_non_finite_missing_and_off_globe_points_are_refused(self) -> None:
        for latitude, longitude in (
            (math.nan, 1.0),
            (1.0, math.inf),
            (None, 1.0),
            ("forty", 1.0),
            (True, 1.0),
            (90.5, 1.0),
            (1.0, -180.5),
        ):
            with (
                self.subTest(latitude=latitude, longitude=longitude),
                self.assertRaises(ImpossibleInputError) as caught,
            ):
                require_coordinates(SERVICE, latitude, longitude)
            self.assertIs(caught.exception.reason, InputRejection.INVALID_COORDINATES)

    def test_null_island_is_refused_unless_the_source_answers_at_sea(self) -> None:
        with self.assertRaises(ImpossibleInputError) as caught:
            require_coordinates(SERVICE, 0.0, -0.0)
        self.assertIs(caught.exception.reason, InputRejection.NULL_ISLAND)
        self.assertEqual(require_coordinates(SERVICE, 0, 0, allow_null_island=True), (0.0, 0.0))

    def test_the_predicate_counts_nothing(self) -> None:
        self.assertIsNotNone(coordinate_problem(math.nan, 0.0))
        self.assertIsNone(coordinate_problem(40.7, -74.0))
        self.assertFalse(ApiCallLog.objects.filter(service=SERVICE).exists())


class RequireQueryTests(TestCase):
    def test_a_real_query_is_returned_unchanged(self) -> None:
        self.assertEqual(require_query(SERVICE, " Kirkbride "), " Kirkbride ")

    def test_blank_and_missing_queries_are_refused(self) -> None:
        for blank in ("", "  ", None):
            with self.subTest(blank=blank), self.assertRaises(ImpossibleInputError) as caught:
                require_query(SERVICE, blank)
            self.assertIs(caught.exception.reason, InputRejection.EMPTY_QUERY)

    def test_a_query_longer_than_the_provider_takes_is_refused(self) -> None:
        with self.assertRaises(ImpossibleInputError) as caught:
            require_query(SERVICE, "x" * 11, max_length=10)
        self.assertIs(caught.exception.reason, InputRejection.OUT_OF_RANGE)


class RequireInRangeFormatDateTests(TestCase):
    def test_range_bounds(self) -> None:
        self.assertEqual(
            require_in_range(SERVICE, "radius", 50_000, minimum=0, maximum=50_000, exclusive_minimum=True), 50_000
        )
        for value in (0, 50_001, math.nan):
            with self.subTest(value=value), self.assertRaises(ImpossibleInputError):
                require_in_range(SERVICE, "radius", value, minimum=0, maximum=50_000, exclusive_minimum=True)

    def test_format_matches_the_whole_value(self) -> None:
        pattern = re.compile(r"[0-9a-f]{4}")
        self.assertEqual(require_format(SERVICE, "hash", "beef", pattern), "beef")
        for value in ("beef0", "BEEF", None):
            with self.subTest(value=value), self.assertRaises(ImpossibleInputError) as caught:
                require_format(SERVICE, "hash", value, pattern)
            self.assertIs(caught.exception.reason, InputRejection.MALFORMED_ID)

    def test_dates_outside_coverage(self) -> None:
        self.assertEqual(
            require_date_within(SERVICE, "start", date(1999, 1, 1), earliest=date(1940, 1, 1)), date(1999, 1, 1)
        )
        with self.assertRaises(ImpossibleInputError) as caught:
            require_date_within(SERVICE, "start", date(1939, 1, 1), earliest=date(1940, 1, 1))
        self.assertIs(caught.exception.reason, InputRejection.OUTSIDE_DATE_COVERAGE)


class RequireJpegSizeTests(TestCase):
    @staticmethod
    def _jpeg(width: int, height: int) -> bytes:
        buffer = io.BytesIO()
        PILImage.new("RGB", (width, height)).save(buffer, format="JPEG")
        return buffer.getvalue()

    def test_a_jpeg_at_the_minimum_is_returned_unchanged(self) -> None:
        for size in ((4, 4), (4, 512), (512, 4)):
            jpeg = self._jpeg(*size)
            with self.subTest(size=size):
                self.assertIs(require_jpeg_size(SERVICE, "image", jpeg, minimum_side=4), jpeg)

    def test_a_jpeg_under_the_minimum_on_either_side_is_out_of_range(self) -> None:
        for size in ((3, 4), (4, 3), (1, 1)):
            with self.subTest(size=size), self.assertRaises(ImpossibleInputError) as caught:
                require_jpeg_size(SERVICE, "image", self._jpeg(*size), minimum_side=4)
            self.assertIs(caught.exception.reason, InputRejection.OUT_OF_RANGE)
            self.assertIn(f"{size[0]}x{size[1]}", caught.exception.detail)

    def test_a_size_the_header_does_not_give_is_left_to_the_provider(self) -> None:
        for content in (b"", b"not-a-jpeg", self._jpeg(1, 1)[:40]):
            with self.subTest(content=content[:12]):
                self.assertIs(require_jpeg_size(SERVICE, "image", content, minimum_side=4), content)
        self.assertFalse(ApiCallLog.objects.filter(service=SERVICE).exists())


class RejectionIsCountedTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        input_validation._last_logged.clear()

    def test_each_rejection_writes_one_flagged_row_naming_its_reason(self) -> None:
        with self.assertRaises(ImpossibleInputError):
            require_query(SERVICE, "")

        row = ApiCallLog.objects.get(service=SERVICE)
        self.assertTrue(row.was_rejected_input)
        self.assertEqual(row.endpoint, "rejected:empty_query")

    def test_rejected_rows_spend_none_of_the_budget(self) -> None:
        ApiRateLimit.objects.update_or_create(
            service=SERVICE,
            defaults={
                "display_name": "t",
                "calls_per_minute": 1,
                "calls_per_day": 1,
                "calls_per_30_days": 1,
                "enabled": True,
            },
        )
        for _ in range(3):
            with self.assertRaises(ImpossibleInputError):
                require_query(SERVICE, " ")

        self.assertEqual(ApiCallLog.objects.for_service(SERVICE).billable().count(), 0)
        self.assertTrue(check_rate_limit(SERVICE))

    def test_rejected_rows_are_their_own_column_in_the_usage_summary(self) -> None:
        with self.assertRaises(ImpossibleInputError):
            require_query(SERVICE, "")

        summary = {entry["service"]: entry for entry in ApiCallLog.objects.summary_by_service()}
        self.assertEqual(summary[SERVICE]["rejected_inputs"], 1)
        self.assertEqual(summary[SERVICE]["errors"], 0)

    def test_the_first_rejection_is_logged_at_info_and_repeats_only_at_debug(self) -> None:
        with self.assertLogs("urbanlens.dashboard.services.core.input_validation", level=logging.DEBUG) as logs:
            for _ in range(3):
                with self.assertRaises(ImpossibleInputError):
                    require_query(SERVICE, "")

        self.assertEqual([record.levelno for record in logs.records], [logging.INFO, logging.DEBUG, logging.DEBUG])

    def test_the_log_line_never_carries_the_input(self) -> None:
        with (
            self.assertLogs("urbanlens.dashboard.services.core.input_validation", level=logging.DEBUG) as logs,
            self.assertRaises(ImpossibleInputError),
        ):
            require_query(SERVICE, "x" * 30, max_length=5)
        self.assertNotIn("x" * 30, "\n".join(logs.output))


class CheckRequestParametersTests(TestCase):
    def test_ordinary_parameters_pass(self) -> None:
        check_request_parameters(SERVICE, params={"lat": "40.7", "lon": -74.0, "q": "nan"})
        check_request_parameters(SERVICE, params=[("latitude", 0), ("longitude", 0)])
        check_request_parameters(SERVICE, json={"textQuery": "asylum", "maxResultCount": 20})
        check_request_parameters(SERVICE, params="lat=nan")

    def test_named_coordinates_that_cannot_be_on_the_globe_are_refused(self) -> None:
        for params in ({"lat": "nan", "lng": 1}, {"latitude": math.inf}, {"lat": 91}, {"longitude": "-181"}):
            with self.subTest(params=params), self.assertRaises(ImpossibleInputError):
                check_request_parameters(SERVICE, params=params)

    def test_an_unparseable_named_coordinate_is_left_to_the_provider(self) -> None:
        check_request_parameters(SERVICE, params={"lat": "40d42m", "lng": ""})

    def test_a_non_finite_float_anywhere_is_refused(self) -> None:
        with self.assertRaises(ImpossibleInputError):
            check_request_parameters(SERVICE, params={"radius": math.nan})
        with self.assertRaises(ImpossibleInputError):
            check_request_parameters(SERVICE, json={"circle": {"center": {"latitude": math.nan, "longitude": 1.0}}})


class SessionRejectsBeforeAnyAccountingTests(TestCase):
    def test_an_impossible_parameter_never_reaches_the_limiter_or_the_network(self) -> None:
        session = _RateLimitedSession(SERVICE)
        with (
            mock.patch.object(session._session, "request") as request,
            mock.patch("urbanlens.dashboard.services.core.rate_limiter._reserve_call") as reserve,
            self.assertRaises(ImpossibleInputError),
        ):
            session.get("https://example.test/near", params={"lat": "nan", "lng": "1"})

        request.assert_not_called()
        reserve.assert_not_called()
        row = ApiCallLog.objects.get(service=SERVICE)
        self.assertTrue(row.was_rejected_input)
        self.assertFalse(row.was_rate_limited)

    def test_an_ordinary_request_still_goes_out(self) -> None:
        session = _RateLimitedSession(SERVICE)
        response = mock.Mock(ok=True, status_code=200, headers={}, url="https://example.test/near")
        with mock.patch.object(session._session, "request", return_value=response) as request:
            session.get("https://example.test/near", params={"lat": "40.7", "lng": "-74.0"})
        request.assert_called_once()
