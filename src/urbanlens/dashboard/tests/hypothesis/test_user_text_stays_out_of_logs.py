"""What a user wrote - a prompt, a model's answer about it, a search, a direct message - never reaches a log line.

Pin names name undisclosed sites (``services.security.redact.redact_text``), and every one of these carries them.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

from django.db import DatabaseError
from urbanlens_ai.schema import InferenceResponse, TextBlock, Usage

from urbanlens.core.tests.ai_guard import real_ai_chokepoint
from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.services.ai.anthropic import AnthropicGateway
from urbanlens.dashboard.services.global_search.engine import GlobalSearchEngine
from urbanlens.dashboard.services.global_search.providers import SearchProvider
from urbanlens.dashboard.services.messaging import dm_location_detection
from urbanlens.dashboard.services.security.redact import redact_urls

_PIN_NAME = "Quokkabridge Sanatorium"
_ANSWER_TEXT = "Zanzibar Marmalade Works"
_ADDRESS = "1847 Wombatgrove Street"
_COORDINATES = "41.718253, -73.934017"


def _text(records: list[logging.LogRecord]) -> str:
    """Every captured record as it would be written, traceback included."""
    formatter = logging.Formatter("%(message)s")
    return "\n".join(formatter.format(record) for record in records)


class AnAnswerlessReplyTests(SimpleTestCase):
    """A reply with no ``<ANSWER>`` tag is logged, and so used to be the prompt and the reply."""

    def setUp(self) -> None:
        self.enterContext(real_ai_chokepoint("urbanlens.dashboard.services.ai.gateway.LLMGateway.send_prompt"))
        self.enterContext(mock.patch.object(AnthropicGateway, "calculate_tokens", return_value=10))
        self.enterContext(
            mock.patch("urbanlens.dashboard.services.ai.scanner.scan", return_value=SimpleNamespace(risk_score=0.0))
        )

    def test_neither_the_prompt_nor_the_reply_is_logged(self) -> None:
        gateway = AnthropicGateway()
        gateway._inference_client = mock.Mock()  # noqa: SLF001 - the seam the gateway's own tests use
        gateway._inference_client.send.return_value = InferenceResponse(  # noqa: SLF001
            content=[TextBlock(text=f"I think it is {_ANSWER_TEXT}.")],
            stop_reason="end_turn",
            usage=Usage(output_tokens=5),
        )

        with self.assertLogs("urbanlens", level="DEBUG") as captured:
            self.assertIsNone(gateway.send_prompt(f"Suggest labels for my pin {_PIN_NAME}"))

        logged = _text(captured.records)
        self.assertIn("ANSWER", logged, "the missing answer was not logged at all")
        self.assertNotIn(_PIN_NAME, logged)
        self.assertNotIn(_ANSWER_TEXT, logged)


class _FailingProvider(SearchProvider):
    slug = "pins"

    def search(self, profile, parsed, limit):  # noqa: ANN001, ANN201, ARG002 - the abstract signature
        raise RuntimeError("provider exploded")


class AFailedSearchTests(SimpleTestCase):
    def test_the_query_is_not_logged(self) -> None:
        with self.assertLogs("urbanlens", level="DEBUG") as captured:
            response = GlobalSearchEngine(providers=[_FailingProvider()]).search(mock.Mock(), _PIN_NAME)

        logged = _text(captured.records)
        self.assertTrue(response.errors, "the failing provider was not reported")
        self.assertIn("pins", logged, "the failure was not logged with its provider")
        self.assertNotIn("quokkabridge", logged.casefold())


class ADirectMessageMentionTests(SimpleTestCase):
    """A direct message's text is the most private thing the app holds."""

    def _message(self, body: str) -> DirectMessage:
        return DirectMessage(pk=7, body=body)

    def test_a_failed_geocode_does_not_log_the_address(self) -> None:
        gateway = mock.Mock()
        gateway.return_value.geocode_place_name.side_effect = ValueError(f"no geocode for {_ADDRESS}")
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway", gateway),
            mock.patch("urbanlens.UrbanLens.settings.app.settings.google_unrestricted_api_key", "configured"),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            self.assertIsNone(dm_location_detection._geocode_address(_ADDRESS))  # noqa: SLF001 - the function that logged it

        logged = _text(captured.records)
        self.assertIn("ValueError", logged, "the failure was not logged with its type")
        self.assertNotIn("Wombatgrove", logged)

    def test_an_unresolvable_address_does_not_log_the_address(self) -> None:
        with (
            mock.patch.object(dm_location_detection, "parse_addresses", return_value=[_ADDRESS]),
            mock.patch.object(dm_location_detection, "_geocode_address", return_value=(41.7, -73.9)),
            mock.patch(
                "urbanlens.dashboard.models.location.model.Location.objects.get_nearby_or_create",
                side_effect=DatabaseError("down"),
            ),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            self.assertEqual(dm_location_detection.detect_address_mentions(self._message(f"meet at {_ADDRESS}")), [])

        self.assertNotIn("Wombatgrove", _text(captured.records))

    def test_unresolvable_coordinates_are_not_logged(self) -> None:
        with (
            mock.patch(
                "urbanlens.dashboard.models.location.model.Location.objects.get_nearby_or_create",
                side_effect=DatabaseError("down"),
            ),
            self.assertLogs("urbanlens", level="DEBUG") as captured,
        ):
            self.assertEqual(
                dm_location_detection.detect_coordinate_mentions(self._message(f"we're at {_COORDINATES} now")), []
            )

        logged = _text(captured.records)
        self.assertNotIn("41.718253", logged)
        self.assertNotIn("73.934017", logged)


class ASearchTermInALoggedUrlTests(SimpleTestCase):
    """A ``requests`` error's text is its URL, so a search a gateway sent is in every traceback that names it."""

    def test_query_parameters_carrying_search_text_are_redacted(self) -> None:
        for name in ("q", "query", "text", "address", "srsearch", "tags"):
            with self.subTest(name=name):
                redacted = redact_urls(
                    f"404 Client Error for url: https://search.example.test/api?{name}=Quokkabridge+Sanatorium&format=json"
                )

                self.assertNotIn("Quokkabridge", redacted)
                self.assertIn("format=json", redacted, "a parameter carrying nothing private was redacted too")
