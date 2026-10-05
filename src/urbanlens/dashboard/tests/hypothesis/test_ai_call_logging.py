"""Every AI call writes exactly one ``ApiCallLog`` row: service, model, time, status, tokens, cost - never content.

Each test drives a real AI path with only the inference client (the one thing that leaves the
process) replaced, and the private text the path puts in its prompt set to a marker that must not
reach any column of the row.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.ai_guard import AI_CHOKEPOINTS, real_ai_chokepoint
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.subscriptions import SiteFeature
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.ai import vision
from urbanlens.dashboard.services.ai.factory import get_gateway
from urbanlens.dashboard.services.ai.inference_client import (
    ClassificationLabel,
    ClassifyResponse,
    InferenceError,
    InferenceResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from urbanlens.dashboard.services.core.rate_limiter import api_call_slot

#: Private text each path sends the model. If it turns up in a row, content was logged.
SECRET = "Zq7-private-Zq7"
CLIENT_PATH = "urbanlens.dashboard.services.ai.inference_client.get_inference_client"
USAGE = Usage(input_tokens=1200, output_tokens=300)


def _answer(text: str, usage: Usage = USAGE) -> InferenceResponse:
    return InferenceResponse(content=[TextBlock(text=text)], stop_reason="end_turn", usage=usage)


def _client(*responses: InferenceResponse | Exception) -> mock.Mock:
    """An inference client answering ``send`` with each response in turn."""
    client = mock.Mock()
    client.send.side_effect = list(responses)
    client.classify.return_value = ClassifyResponse(labels=[ClassificationLabel(label="mill", score=0.9)])
    return client


def _grant_ai() -> None:
    site = SiteSettings.get_current()
    SiteSettings.objects.filter(pk=site.pk).update(default_features=SiteFeature.AI)


def _profile() -> Profile:
    baker.make(User)  # the first user is promoted to site admin
    _grant_ai()
    return Profile.objects.get(user=baker.make(User))


class _LoggingCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        # The suite stubs the gateway's chokepoints, and these tests are about what they do. Token counting is
        # tiktoken, which fetches its encoding over the network on a host without the image's baked cache.
        for target in AI_CHOKEPOINTS:
            self.enterContext(real_ai_chokepoint(target))
        self.enterContext(
            mock.patch("urbanlens.dashboard.services.ai.gateway.LLMGateway.calculate_tokens", return_value=10)
        )

    def one_row(self, service: str) -> ApiCallLog:
        """The only row ``service`` wrote, with nothing private in it."""
        rows = list(ApiCallLog.objects.filter(service=service))
        self.assertEqual(len(rows), 1, f"{service}: {len(rows)} rows for one call")
        row = rows[0]
        for name in ("service", "endpoint", "model"):
            self.assertNotIn(SECRET, str(getattr(row, name) or ""), f"{name} carries the prompt")
        return row

    def assert_ledgered(self, service: str, *, model: str | None = None) -> ApiCallLog:
        row = self.one_row(service)
        self.assertTrue(row.success)
        self.assertTrue(row.model, "the model id is not recorded")
        if model is not None:
            self.assertEqual(row.model, model)
        self.assertEqual((row.input_tokens, row.output_tokens), (1200, 300))
        self.assertIsNotNone(row.response_ms)
        return row


class GatewayCallTests(_LoggingCase):
    """A call through any provider's gateway writes one row, success or failure."""

    def test_every_provider_logs_its_calls_under_the_feature_and_the_model(self) -> None:
        for provider in ("cloudflare", "openai", "anthropic"):
            with (
                self.subTest(provider=provider),
                mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>"))),
            ):
                gateway = get_gateway("feature_" + provider, provider=provider)
                assert gateway is not None
                self.assertEqual(gateway.send_prompt(f"describe {SECRET}"), "x")

                row = self.assert_ledgered("feature_" + provider, model=gateway.model)
                self.assertEqual(row.endpoint, f"{provider}:{gateway.model}")
                self.assertFalse(row.was_rate_limited)

    def test_a_gateway_without_a_feature_logs_under_its_provider(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>"))):
            gateway = get_gateway(provider="cloudflare")
            assert gateway is not None
            gateway.send_prompt("hello")

        self.assert_ledgered("ai_cloudflare")

    def test_a_list_prompt_is_one_call(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>a</ANSWER><ANSWER>b</ANSWER>"))):
            gateway = get_gateway("list_feature")
            assert gateway is not None
            self.assertEqual(gateway.send_prompt_list(SECRET), ["a", "b"])

        self.assert_ledgered("list_feature")

    def test_a_tool_round_is_one_call(self) -> None:
        reply = InferenceResponse(
            content=[ToolUseBlock(id="t", name="n", input={})], stop_reason="tool_use", usage=USAGE
        )
        with mock.patch(CLIENT_PATH, return_value=_client(reply)):
            gateway = get_gateway("tool_feature", provider="anthropic")
            assert gateway is not None
            gateway.send_with_tools(SECRET, [])

        self.assert_ledgered("tool_feature")

    def test_a_failed_call_is_logged_with_the_status_it_failed_with(self) -> None:
        failure = InferenceError("ai-inference returned HTTP 502")
        failure.status_code = 502
        with mock.patch(CLIENT_PATH, return_value=_client(failure)):
            gateway = get_gateway("failing_feature")
            assert gateway is not None
            self.assertIsNone(gateway.send_prompt(SECRET))

        row = self.one_row("failing_feature")
        self.assertFalse(row.success)
        self.assertEqual(row.status_code, 502)
        self.assertTrue(row.model)
        self.assertIsNone(row.input_tokens)
        self.assertIsNone(row.cost_estimate)

    def test_only_a_server_failure_records_a_status(self) -> None:
        """A 401 from ai-inference is our credential refused; recorded, provider_health would read it as the provider saying no."""
        refused = InferenceError("ai-inference returned HTTP 401", status_code=401)
        with mock.patch(CLIENT_PATH, return_value=_client(refused)):
            gateway = get_gateway("refused_feature")
            assert gateway is not None
            self.assertIsNone(gateway.send_prompt(SECRET))

        row = self.one_row("refused_feature")
        self.assertFalse(row.success)
        self.assertIsNone(row.status_code)

    def test_a_call_under_another_features_slot_is_its_own_row(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>"))):
            gateway = get_gateway("inner_feature")
            assert gateway is not None
            with api_call_slot("outer_feature") as slot:
                slot.success = gateway.send_prompt(SECRET) is not None

        self.assert_ledgered("inner_feature")
        self.assertIsNone(ApiCallLog.objects.get(service="outer_feature").model)

    def test_a_call_that_made_no_request_logs_nothing(self) -> None:
        client = _client()
        with mock.patch(CLIENT_PATH, return_value=client):
            gateway = get_gateway("oversized_feature")
            assert gateway is not None
            with mock.patch.object(type(gateway), "construct_messages", side_effect=ValueError("too long")):
                self.assertIsNone(gateway.send_prompt(SECRET))

        client.send.assert_not_called()
        self.assertFalse(ApiCallLog.objects.filter(service="oversized_feature").exists())

    def test_the_cost_is_this_calls_own_and_survives_a_cheap_model(self) -> None:
        with mock.patch(
            CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>"), _answer("<ANSWER>y</ANSWER>"))
        ):
            gateway = get_gateway("priced_feature", provider="openai")
            assert gateway is not None
            gateway.send_prompt("one")
            gateway.send_prompt("two")

        costs = [row.cost_estimate for row in ApiCallLog.objects.filter(service="priced_feature")]
        # gpt-5-nano at $0.00005 / $0.0004 per thousand: 1200 in + 300 out is $0.00018 a call.
        self.assertEqual(costs, [Decimal("0.000180")] * 2)

    def test_a_response_without_usage_leaves_the_token_columns_empty(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>", Usage()))):
            gateway = get_gateway("quiet_feature")
            assert gateway is not None
            gateway.send_prompt("hello")

        row = self.one_row("quiet_feature")
        self.assertTrue(row.success)
        self.assertIsNone(row.input_tokens)
        self.assertIsNone(row.output_tokens)

    def test_a_logging_failure_never_fails_the_call(self) -> None:
        with (
            mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>"))),
            mock.patch(
                "urbanlens.dashboard.models.api_call_log.model.ApiCallLog.objects.create",
                side_effect=RuntimeError("db down"),
            ),
        ):
            gateway = get_gateway("unlogged_feature")
            assert gateway is not None
            self.assertEqual(gateway.send_prompt("hello"), "x")

    def test_a_reserved_call_keeps_its_one_row_and_gains_the_model(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("<ANSWER>x</ANSWER>"))):
            gateway = get_gateway("slotted_feature")
            assert gateway is not None
            with api_call_slot("slotted_feature", endpoint=gateway.model) as slot:
                slot.success = gateway.send_prompt(SECRET) is not None

        row = self.assert_ledgered("slotted_feature", model=gateway.model)
        self.assertEqual(row.endpoint, f"cloudflare:{gateway.model}")


class TextPathTests(_LoggingCase):
    """Each text path's own row: the six that wrote none, and the ones that wrote one without the model."""

    def run_path(self, text: str = "<ANSWER>x</ANSWER>") -> mock.Mock:
        client = _client(_answer(text))
        patcher = mock.patch(CLIENT_PATH, return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client

    def test_label_style_suggestions(self) -> None:
        from urbanlens.dashboard.services.labels.style_suggestions import suggest_label_style

        profile = _profile()
        self.run_path("<ANSWER>x</ANSWER>")
        suggest_label_style(f"{SECRET} Factories", profile)

        self.assert_ledgered("label_style_suggestions")

    def test_category_suggestions(self) -> None:
        from urbanlens.dashboard.models.labels.model import Label
        from urbanlens.dashboard.services.labels.auto_tag import AutoTagService

        self.run_path("<ANSWER>Factory</ANSWER>")
        label = baker.make(Label, name="Factory")
        AutoTagService()._ai_match(SimpleNamespace(address=f"{SECRET} Road"), [label], "category")

        self.assert_ledgered("category_suggestions")

    def test_document_pin_import(self) -> None:
        from urbanlens.dashboard.services.ai.document_import import extract_pins_from_text

        profile = _profile()
        self.run_path("<ANSWER>nothing</ANSWER>")
        extract_pins_from_text("notes.txt", f"We went to {SECRET} mill.", profile)

        self.assert_ledgered("document_pin_import")

    def test_link_extraction(self) -> None:
        from urbanlens.dashboard.models.link_extraction.model import LinkExtraction
        from urbanlens.dashboard.models.pin.model import Pin
        from urbanlens.dashboard.services.ai.link_extraction import run_extraction

        profile = _profile()
        pin = baker.make(Pin, profile=profile, name=f"{SECRET} Mill", name_is_user_provided=True)
        extraction = LinkExtraction.objects.create(profile=profile, pin=pin, url="https://example.com/history")
        self.run_path('<ANSWER>{"owner_name": null}</ANSWER>')
        with (
            mock.patch(
                "urbanlens.dashboard.services.ai.link_extraction.fetch_page_text", return_value=f"page about {SECRET}"
            ),
            mock.patch("urbanlens.dashboard.services.ai.article_expansion.expand_articles_from_page", return_value=[]),
        ):
            run_extraction(extraction)

        self.assert_ledgered("link_extraction")

    def test_trip_suggestions(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership
        from urbanlens.dashboard.services.trips.trip_ai_suggestions import generate_trip_suggestions

        profile = _profile()
        trip = baker.make(Trip, name=f"{SECRET} Trip", creator=profile)
        baker.make(TripMembership, trip=trip, profile=profile, status=TripMembership.STATUS_JOINED, rsvp="yes")
        self.run_path('<ANSWER>{"summary": "fine", "pin_suggestions": [], "schedule": null}</ANSWER>')
        generate_trip_suggestions(trip, profile)

        self.assert_ledgered("trip_suggestions")

    def test_trivia_generation(self) -> None:
        from urbanlens.dashboard.services.trivia.generation import generate_questions_for_wiki

        wiki = baker.make(
            Wiki, location=baker.make(Location), description=f"{SECRET} was a mill with a long history. " * 20
        )
        self.run_path("<ANSWER>no separator</ANSWER>")
        generate_questions_for_wiki(wiki)

        self.assert_ledgered("trivia_generation")

    def test_trivia_moderation(self) -> None:
        from urbanlens.dashboard.services.trivia.classifier import classify_trivia_question

        self.run_path("<ANSWER>APPROVE</ANSWER>")
        classify_trivia_question(f"{SECRET}?", "1912", baker.make(Location))

        self.assert_ledgered("trivia_moderation")

    def test_trivia_answer_check(self) -> None:
        from urbanlens.dashboard.services.trivia.answer_check import is_answer_equivalent

        profile = _profile()
        self.run_path("<ANSWER>MATCH</ANSWER>")
        is_answer_equivalent(SECRET, "1912", profile=profile)

        self.assert_ledgered("trivia_answer_check")

    def test_trivia_wiki_incorporation(self) -> None:
        from urbanlens.dashboard.services.trivia.wiki_incorporation import _draft_paragraph

        self.run_path("<ANSWER>A paragraph.</ANSWER>")
        _draft_paragraph(place_name="Mill", prompt=f"{SECRET}?", answer="1912", existing_article="")

        self.assert_ledgered("trivia_wiki_incorporation")

    def test_article_expansion(self) -> None:
        from urbanlens.dashboard.services.ai.article_expansion import _draft_new_paragraphs

        self.run_path("<ANSWER>A paragraph.</ANSWER>")
        _draft_new_paragraphs(
            place_name="Mill", page_text=f"page {SECRET}", pin_article="", wiki_article=None, wiki=None, profile=None
        )

        self.assert_ledgered("article_expansion")

    def test_article_safety(self) -> None:
        from urbanlens.dashboard.services.ai.article_safety import classify_article_text

        self.run_path("<ANSWER>APPROVE</ANSWER>")
        classify_article_text(f"text {SECRET}", place_name="Mill")

        self.assert_ledgered("article_safety")

    def test_the_assistant_logs_each_provider_round(self) -> None:
        from urbanlens.dashboard.services.ai.assistant import run_assistant_turn

        profile = _profile()
        tool = InferenceResponse(
            content=[ToolUseBlock(id="t1", name="list_trips", input={})], stop_reason="tool_use", usage=USAGE
        )
        client = _client(tool, _answer("Here are your trips."))
        with mock.patch(CLIENT_PATH, return_value=client):
            run_assistant_turn(profile, [], f"what about {SECRET}?")

        rows = list(ApiCallLog.objects.filter(service="assistant"))
        self.assertEqual(len(rows), 2, "one row for each call to the provider")
        for row in rows:
            self.assertTrue(row.success)
            self.assertTrue(row.model)
            self.assertEqual((row.input_tokens, row.output_tokens), (1200, 300))
            self.assertNotIn(SECRET, f"{row.endpoint}{row.model}")

    def test_a_failed_assistant_call_is_one_failed_row(self) -> None:
        from urbanlens.dashboard.services.ai.assistant import run_assistant_turn

        profile = _profile()
        with mock.patch(CLIENT_PATH, return_value=_client(InferenceError("boom"))):
            run_assistant_turn(profile, [], "hi")

        row = self.one_row("assistant")
        self.assertFalse(row.success)
        self.assertTrue(row.model)


class VisionTests(_LoggingCase):
    """The image calls log the model, and never the image."""

    def test_photo_keywords(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("brick, mill"))):
            self.assertEqual(vision.describe_photo_keywords(SECRET.encode()), ["brick", "mill"])

        row = self.assert_ledgered(vision.SERVICE_AI_PHOTO_KEYWORDS)
        self.assertEqual(row.model, vision._CF_VISION_MODEL)
        self.assertEqual(row.endpoint, f"cloudflare:{vision._CF_VISION_MODEL}")

    def test_photo_keywords_on_openai(self) -> None:
        SiteSettings.objects.filter(pk=SiteSettings.get_current().pk).update(
            ai_provider="openai", openai_model="gpt-5-mini"
        )
        with mock.patch(CLIENT_PATH, return_value=_client(_answer("brick"))):
            vision.describe_photo_keywords(SECRET.encode())

        row = self.assert_ledgered(vision.SERVICE_AI_PHOTO_KEYWORDS, model="gpt-5-mini")
        self.assertEqual(row.endpoint, "openai:gpt-5-mini")

    def test_a_failed_vision_call_keeps_the_model(self) -> None:
        failure = InferenceError("ai-inference returned HTTP 502")
        failure.status_code = 502
        with mock.patch(CLIENT_PATH, return_value=_client(failure)):
            vision.describe_photo_keywords(SECRET.encode())

        row = self.one_row(vision.SERVICE_AI_PHOTO_KEYWORDS)
        self.assertFalse(row.success)
        self.assertEqual(row.model, vision._CF_VISION_MODEL)
        self.assertEqual(row.status_code, 502)

    def test_photo_classifier(self) -> None:
        with mock.patch(CLIENT_PATH, return_value=_client()):
            self.assertEqual(vision.classify_photo(SECRET.encode()), [("mill", 0.9)])

        row = self.one_row(vision.SERVICE_PHOTO_CLASSIFIER)
        self.assertTrue(row.success)
        self.assertEqual(row.model, vision._CF_CLASSIFIER_MODEL)
        self.assertIsNone(row.input_tokens)


class OllamaTests(_LoggingCase):
    """The self-hosted model's call goes through the gateway session and is annotated there."""

    def _wire(self, body: dict) -> mock.Mock:
        response = mock.Mock(ok=True, status_code=200)
        response.json.return_value = body
        response.raise_for_status.return_value = None
        return response

    def test_a_local_vision_call_logs_its_model_and_tokens(self) -> None:
        from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway

        body = {"response": "brick, mill", "prompt_eval_count": 1200, "eval_count": 300}
        with mock.patch("requests.Session.request", return_value=self._wire(body)):
            keywords = OllamaGateway(base_url="http://ollama.test:11434", model="llava").describe_photo_keywords(
                SECRET.encode()
            )

        self.assertEqual(keywords, ["brick", "mill"])
        row = self.assert_ledgered("ollama", model="llava")
        self.assertEqual(row.status_code, 200)

    def test_a_response_without_counts_still_names_the_model(self) -> None:
        from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway

        with mock.patch("requests.Session.request", return_value=self._wire({"response": "brick"})):
            OllamaGateway(base_url="http://ollama.test:11434", model="llava").describe_photo_keywords(SECRET.encode())

        row = self.one_row("ollama")
        self.assertEqual(row.model, "llava")
        self.assertIsNone(row.input_tokens)


class EveryInferenceCallerLogsTests(SimpleTestCase):
    """The inference client is reached from the two modules that record what they send, and no other."""

    def test_nothing_else_calls_the_inference_client(self) -> None:
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[3]
        callers = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*.py")
            if "/tests/" not in path.as_posix()
            and re.search(
                r"get_inference_client\(\)\.(send|classify)\(|_inference_client\.(send|classify)\(", path.read_text()
            )
        }

        self.assertEqual(
            callers,
            {"dashboard/services/ai/gateway.py", "dashboard/services/ai/vision.py"},
            "a new inference caller must record its call: see services/ai/call_log.py",
        )
