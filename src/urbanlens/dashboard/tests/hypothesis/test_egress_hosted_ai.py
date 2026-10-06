"""Development and local call no hosted AI provider (Jess, 2026-10-06, amending D26).

Cloudflare Workers AI, OpenAI and Anthropic are refused where quota and billed services are, at every place a call
passes: the feature's ``api_call_slot`` and ``_reserve_call``, ``service_is_enabled``, and the inference client that
sends the request on to the provider, whether it goes through the ``ai-inference`` service or in-process. A local or
self-hosted model (Ollama) is ``internal`` and stays. Staging and production are unchanged, and
``UL_ENVIRONMENT_SHARE_OVERRIDES`` opts one feature or one provider back in.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from typing import get_args
from unittest import mock

from django.test import override_settings
from urbanlens_ai.schema import ClassifyRequest, ImagePart, InferenceRequest, Message, Provider

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
from urbanlens.dashboard.services.ai.inference_client import (
    InferenceError,
    LocalInferenceClient,
    RemoteInferenceClient,
)
from urbanlens.dashboard.services.core import egress
from urbanlens.dashboard.services.core.egress import (
    HOSTED_AI_PROVIDERS,
    ai_provider_service,
    egress_permitted,
    explicit_category,
    require_ai_provider,
    require_egress,
)
from urbanlens.dashboard.services.core.rate_limiter import (
    EnvironmentRefusedError,
    _reserve_call,
    all_service_defaults,
    api_call_slot,
    service_is_enabled,
)
from urbanlens.UrbanLens.egress import EgressCategory

#: Every AI feature's service key: the keys ``api_call_slot`` is opened under for a text or vision call.
AI_FEATURES = (
    "trivia_generation",
    "trivia_moderation",
    "trivia_answer_check",
    "trivia_wiki_incorporation",
    "article_expansion",
    "article_safety",
    "link_extraction",
    "document_pin_import",
    "trip_suggestions",
    "label_style_suggestions",
    "category_suggestions",
    "assistant",
    "ai_photo_keywords",
    "cloudflare_image_classifier",
)
AI_PROVIDERS = ("anthropic", "cloudflare", "openai")
HOP = "urbanlens.dashboard.services.ai.inference_client.requests.post"


@contextlib.contextmanager
def deployment(environment: str, *, overrides: dict[str, float] | None = None) -> Iterator[None]:
    """Pretend to be one deployment: its ``UL_ENVIRONMENT`` and ``UL_ENVIRONMENT_SHARE_OVERRIDES``."""
    with (
        override_settings(ENVIRONMENT_NAME=environment, TESTING=False),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share", None),
        mock.patch("urbanlens.UrbanLens.settings.app.settings.environment_share_overrides", overrides or {}),
    ):
        yield


def _request(provider: str = "cloudflare") -> InferenceRequest:
    return InferenceRequest(provider=provider, model="m", messages=[Message(role="user", content="hi")], max_tokens=10)


def _classify(provider: str = "cloudflare") -> ClassifyRequest:
    return ClassifyRequest(provider=provider, model="m", image=ImagePart(data="AAAA"))


class WhatIsHostedAiTests(SimpleTestCase):
    """The classification: every feature and provider that reaches a hosted model is ``ai``; a local one is not."""

    def test_every_ai_feature_is_classified_ai(self) -> None:
        for feature in AI_FEATURES:
            with self.subTest(feature=feature):
                self.assertIs(explicit_category(feature), EgressCategory.AI)

    def test_nothing_else_is_ai_but_the_providers(self) -> None:
        """A new ``category=EgressCategory.AI`` is a new feature; it joins AI_FEATURES, and this test says so."""
        in_registry = {
            service for service, defaults in all_service_defaults().items() if defaults.category is EgressCategory.AI
        }
        self.assertEqual(in_registry, set(AI_FEATURES))

    def test_every_hosted_provider_has_a_key_and_is_ai(self) -> None:
        self.assertEqual(set(HOSTED_AI_PROVIDERS), set(AI_PROVIDERS))
        for provider in HOSTED_AI_PROVIDERS:
            with self.subTest(provider=provider):
                self.assertEqual(ai_provider_service(provider), f"ai_{provider}")
                self.assertIs(explicit_category(ai_provider_service(provider)), EgressCategory.AI)

    def test_every_provider_the_inference_service_knows_is_a_hosted_one(self) -> None:
        """A provider added to ``urbanlens_ai.schema.Provider`` is held here until someone says whether it is hosted.

        A local model is added to neither list: it gets an ``internal`` key, and this test is where that is decided.
        """
        self.assertEqual(set(get_args(Provider)), set(HOSTED_AI_PROVIDERS))

    def test_ollama_and_the_inference_transport_are_not_hosted_ai(self) -> None:
        self.assertIs(explicit_category("ollama"), EgressCategory.INTERNAL)
        self.assertIs(explicit_category("ai_inference"), EgressCategory.INTERNAL)


class DevelopmentRefusesHostedAiTests(TestCase):
    def test_every_ai_feature_is_refused_before_anything_is_recorded(self) -> None:
        for environment in ("development", "local"):
            with deployment(environment):
                for feature in AI_FEATURES:
                    with (
                        self.subTest(environment=environment, feature=feature),
                        self.assertRaises(EnvironmentRefusedError) as raised,
                    ):
                        _reserve_call(feature, endpoint="x")
                    self.assertEqual(raised.exception.category, "ai")
                    self.assertEqual(raised.exception.environment, environment)
        self.assertFalse(ApiCallLog.objects.exists())
        self.assertFalse(ApiRateLimit.objects.filter(service__in=AI_FEATURES).exists())

    def test_the_slot_refuses_before_the_block_runs(self) -> None:
        ran = False
        with deployment("development"), self.assertRaises(EnvironmentRefusedError), api_call_slot("link_extraction"):
            ran = True
        self.assertFalse(ran)
        self.assertFalse(ApiCallLog.objects.exists())

    def test_the_question_asked_ahead_of_time_says_no(self) -> None:
        """The UI asks before offering a button; a held row in hand does not change the answer."""
        enabled_row = mock.Mock(enabled=True)
        with deployment("development"):
            for feature in AI_FEATURES:
                with self.subTest(feature=feature):
                    self.assertFalse(egress_permitted(feature))
                    self.assertFalse(service_is_enabled(feature, config=enabled_row))

    def test_a_local_model_is_still_called(self) -> None:
        with deployment("development"):
            require_egress("ollama")
            require_egress("ai_inference")
            _reserve_call("ollama", endpoint="x")
        self.assertEqual(ApiCallLog.objects.filter(service="ollama").count(), 1)

    def test_staging_and_production_are_unchanged(self) -> None:
        for environment in ("staging", "production"):
            with deployment(environment):
                for feature in AI_FEATURES:
                    with self.subTest(environment=environment, feature=feature):
                        self.assertTrue(egress_permitted(feature))
                        _reserve_call(feature, endpoint="x")
        self.assertEqual(ApiCallLog.objects.count(), 2 * len(AI_FEATURES))

    def test_the_test_suite_still_runs_the_ai_features_against_its_mocks(self) -> None:
        for feature in AI_FEATURES:
            with self.subTest(feature=feature):
                self.assertTrue(egress_permitted(feature))


class OverridesReEnableTests(TestCase):
    def test_a_feature_override_opts_that_feature_in_and_no_other(self) -> None:
        with deployment("development", overrides={"trivia_generation": 1.0}):
            _reserve_call("trivia_generation", endpoint="x")
            with self.assertRaises(EnvironmentRefusedError):
                _reserve_call("trivia_moderation", endpoint="x")
            with self.assertRaises(EnvironmentRefusedError):
                _reserve_call("assistant", endpoint="x")

    def test_a_provider_override_lets_every_feature_start(self) -> None:
        """Which provider a feature uses is the site's setting, known only when it calls: the transport has the last word."""
        with deployment("local", overrides={"ai_cloudflare": 1.0}):
            for feature in AI_FEATURES:
                with self.subTest(feature=feature):
                    self.assertTrue(egress_permitted(feature))
                    _reserve_call(feature, endpoint="x")

    def test_a_provider_override_does_not_turn_a_feature_off_that_was_switched_off_by_name(self) -> None:
        with deployment("production", overrides={"assistant": 0.0, "ai_cloudflare": 1.0}):
            self.assertFalse(egress_permitted("assistant"))
            self.assertTrue(egress_permitted("link_extraction"))

    def test_zero_switches_one_feature_off_on_production(self) -> None:
        with deployment("production", overrides={"link_extraction": 0.0}):
            self.assertFalse(egress_permitted("link_extraction"))
            self.assertTrue(egress_permitted("assistant"))

    def test_an_override_naming_something_that_is_no_provider_does_not_open_the_rest(self) -> None:
        with deployment("development", overrides={"nominatim": 1.0, "ollama": 1.0}):
            self.assertFalse(egress_permitted("assistant"))

    def test_the_startup_line_names_the_ai_overrides(self) -> None:
        with deployment("development", overrides={"trivia_generation": 1.0, "ai_openai": 0.5}):
            line = egress.describe_policy()
        self.assertIn("trivia_generation=1", line)
        self.assertIn("ai_openai=0.5", line)

    def test_the_startup_log_accepts_an_ai_override_without_calling_it_ignored(self) -> None:
        with (
            deployment("development", overrides={"trivia_generation": 1.0}),
            self.assertLogs(egress.logger, "INFO") as logs,
        ):
            egress.log_egress_policy()
        self.assertNotIn("no share applies", "\n".join(logs.output))


class TheInferenceClientIsTheLastGateTests(SimpleTestCase):
    """A request that would reach a hosted provider never leaves the process in development."""

    def _remote(self) -> RemoteInferenceClient:
        return RemoteInferenceClient("http://ai-inference", "token", timeout_seconds=5.0)

    def test_the_remote_client_refuses_each_provider_before_the_hop(self) -> None:
        for provider in AI_PROVIDERS:
            with (
                self.subTest(provider=provider),
                deployment("development"),
                mock.patch(HOP) as post,
                self.assertRaises(EnvironmentRefusedError) as raised,
            ):
                self._remote().send(_request(provider))
            post.assert_not_called()
            self.assertEqual(raised.exception.service, ai_provider_service(provider))

    def test_the_remote_client_refuses_a_classification_too(self) -> None:
        with deployment("local"), mock.patch(HOP) as post, self.assertRaises(EnvironmentRefusedError):
            self._remote().classify(_classify())
        post.assert_not_called()

    def test_the_in_process_client_refuses_before_it_builds_an_adapter(self) -> None:
        client = LocalInferenceClient()
        for call, request in ((client.send, _request("openai")), (client.classify, _classify())):
            with (
                self.subTest(call=call.__name__),
                deployment("development"),
                mock.patch("urbanlens_ai.providers.build_adapter") as build,
                self.assertRaises(EnvironmentRefusedError),
            ):
                call(request)
            build.assert_not_called()

    def test_the_refusal_is_not_an_inference_failure(self) -> None:
        """``InferenceError`` is what the gateway logs as a failed provider call and what health reads."""
        with deployment("development"), mock.patch(HOP), self.assertRaises(EnvironmentRefusedError) as raised:
            self._remote().send(_request())
        self.assertNotIsInstance(raised.exception, InferenceError)

    def test_staging_and_production_send(self) -> None:
        for environment in ("staging", "production"):
            for provider in AI_PROVIDERS:
                with (
                    self.subTest(environment=environment, provider=provider),
                    deployment(environment),
                    mock.patch(HOP, return_value=mock.Mock(status_code=500)) as post,
                    self.assertRaises(InferenceError),
                ):
                    self._remote().send(_request(provider))
                post.assert_called_once()

    def test_the_suite_still_reaches_the_client_it_mocks(self) -> None:
        with mock.patch(HOP, return_value=mock.Mock(status_code=500)) as post, self.assertRaises(InferenceError):
            self._remote().send(_request())
        post.assert_called_once()

    def test_a_provider_override_sends_that_provider_only(self) -> None:
        with deployment("development", overrides={"ai_cloudflare": 1.0}):
            with mock.patch(HOP, return_value=mock.Mock(status_code=500)) as post, self.assertRaises(InferenceError):
                self._remote().send(_request("cloudflare"))
            post.assert_called_once()
            with mock.patch(HOP) as post, self.assertRaises(EnvironmentRefusedError):
                self._remote().send(_request("openai"))
            post.assert_not_called()

    def test_a_call_with_no_feature_behind_it_needs_its_provider_opted_in(self) -> None:
        with (
            deployment("development", overrides={"trivia_generation": 1.0}),
            self.assertRaises(EnvironmentRefusedError),
        ):
            require_ai_provider("cloudflare")

    def test_the_refusal_is_collected_like_any_other(self) -> None:
        with (
            deployment("development"),
            egress.collect_refusals() as refused,
            contextlib.suppress(EnvironmentRefusedError),
        ):
            require_ai_provider("anthropic")
        self.assertEqual(refused, ["ai_anthropic"])


class TheFeatureOverrideCarriesToItsProviderTests(TestCase):
    """Inside a feature's slot, with that feature opted in, the provider behind it is too."""

    def test_a_refusal_inside_the_slot_leaves_no_ledger_row(self) -> None:
        """Let start by the provider override, refused at the transport: nothing was sent, so nothing is recorded."""
        with (
            deployment("development", overrides={"ai_anthropic": 1.0}),
            mock.patch(HOP) as post,
            contextlib.suppress(EnvironmentRefusedError),
            api_call_slot("assistant"),
        ):
            self._send("cloudflare")
        post.assert_not_called()
        self.assertFalse(ApiCallLog.objects.exists())

    def _send(self, provider: str) -> None:
        RemoteInferenceClient("http://ai-inference", "token", timeout_seconds=5.0).send(_request(provider))

    def test_the_feature_s_own_calls_reach_whichever_provider_the_site_picked(self) -> None:
        with deployment("development", overrides={"trivia_generation": 1.0}):
            for provider in AI_PROVIDERS:
                with (
                    self.subTest(provider=provider),
                    api_call_slot("trivia_generation"),
                    mock.patch(HOP, return_value=mock.Mock(status_code=500)) as post,
                    self.assertRaises(InferenceError),
                ):
                    self._send(provider)
                post.assert_called_once()

    def test_another_feature_s_calls_do_not(self) -> None:
        with (
            deployment("development", overrides={"trivia_generation": 1.0}),
            self.assertRaises(EnvironmentRefusedError),
            api_call_slot("assistant"),
        ):
            self.fail("the assistant was not opted in, so its slot should not open")

    def test_a_provider_override_alone_opens_the_slot_and_that_provider_only(self) -> None:
        with deployment("development", overrides={"ai_anthropic": 1.0}), api_call_slot("assistant"):
            with mock.patch(HOP, return_value=mock.Mock(status_code=500)) as post, self.assertRaises(InferenceError):
                self._send("anthropic")
            post.assert_called_once()
            with mock.patch(HOP) as post, self.assertRaises(EnvironmentRefusedError):
                self._send("cloudflare")
            post.assert_not_called()
