"""Cloudflare's classifier refusing an image is an answer, and a classifier that did not answer stores nothing (P320).

Every failed ``cloudflare_image_classifier`` call in dev's ApiCallLog (237, 2026-09-13 to 2026-09-30) was a tiny test
upload. Cloudflare answered 400 with ``code 3011, "AiError: AiError: image too small, expected image at least 4x4"``;
the adapter kept only ``400 Client Error: Bad Request for url: <the account's gateway URL>``, ai-inference answered
502, and the call was logged failed, which is what provider health backs a provider off for.
"""

from __future__ import annotations

import base64
import importlib
import io
import json
import os
from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.ai import vision
from urbanlens.dashboard.services.ai.inference_client import (
    ClassifyRequest,
    ImagePart,
    InferenceError,
    InferenceInputRefusedError,
    InferenceResponse,
    LocalInferenceClient,
    RemoteInferenceClient,
    TextBlock,
    Usage,
)
from urbanlens.dashboard.services.core.rate_limiter import RequestCancelledError

_ACCOUNT = "0123456789abcdef0123456789abcdef"
_ENDPOINT = f"https://gateway.ai.cloudflare.com/v1/{_ACCOUNT}/urban-lens/workers-ai"
_JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
#: Cloudflare's answer to a 1x1 JPEG, captured from the AI Gateway on 2026-10-05.
_TOO_SMALL = {
    "errors": [
        {
            "message": "AiError: AiError: image too small, expected image at least 4x4 (c69865ef-dd96-49d7-965d-f198456015c2)",
            "code": 3011,
        }
    ],
    "success": False,
    "result": {},
    "messages": [],
}
_NO_SUCH_MODEL = {
    "errors": [{"message": "No such model @cf/microsoft/resnet-51 or task", "code": 5007}],
    "success": False,
}


def _classify_request() -> ClassifyRequest:
    return ClassifyRequest(
        provider="cloudflare",
        model="@cf/microsoft/resnet-50",
        image=ImagePart(data=base64.b64encode(_JPEG).decode("ascii")),
    )


def _cloudflare_answer(status: int, body: dict[str, Any] | str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.reason = "Bad Request" if status == 400 else "Error"
    response.url = f"{_ENDPOINT}/@cf/microsoft/resnet-50"
    response._content = (json.dumps(body) if isinstance(body, dict) else body).encode()
    return response


class CloudflareAnswerTests(SimpleTestCase):
    def _classify(self, answer: requests.Response) -> Exception:
        from urbanlens_ai.providers.base import ProviderError
        from urbanlens_ai.providers.cloudflare import CloudflareAdapter

        with (
            mock.patch("urbanlens_ai.providers.cloudflare.requests.post", return_value=answer),
            self.assertRaises(ProviderError) as raised,
        ):
            CloudflareAdapter("key", _ENDPOINT).classify(_classify_request())
        return raised.exception

    def test_a_too_small_image_is_a_refusal_with_cloudflares_reason(self) -> None:
        from urbanlens_ai.providers.base import ProviderInputRefusedError

        error = self._classify(_cloudflare_answer(400, _TOO_SMALL))

        self.assertIsInstance(error, ProviderInputRefusedError)
        self.assertIn("3011", str(error))
        self.assertIn("image too small", str(error))
        self.assertNotIn(_ACCOUNT, str(error))

    def test_an_unknown_model_is_a_failure_not_a_refusal(self) -> None:
        from urbanlens_ai.providers.base import ProviderInputRefusedError

        error = self._classify(_cloudflare_answer(400, _NO_SUCH_MODEL))

        self.assertNotIsInstance(error, ProviderInputRefusedError)
        self.assertIn("5007", str(error))
        self.assertIn("No such model", str(error))

    def test_an_answer_that_is_not_json_is_a_failure_naming_its_status(self) -> None:
        from urbanlens_ai.providers.base import ProviderInputRefusedError

        error = self._classify(_cloudflare_answer(502, "<html>bad gateway</html>"))

        self.assertNotIsInstance(error, ProviderInputRefusedError)
        self.assertIn("502", str(error))
        self.assertNotIn(_ACCOUNT, str(error))


class InferenceServiceTests(SimpleTestCase):
    """ai-inference answers 422 for a refused input and keeps 502 for a failed call."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens_ai.config import get_config

        patcher = mock.patch.dict(os.environ, {"UL_AI_INFERENCE_TOKEN": "test-token"})
        patcher.start()
        get_config.cache_clear()
        self.addCleanup(get_config.cache_clear)
        self.addCleanup(patcher.stop)
        self.wsgi = importlib.import_module("urbanlens_ai.wsgi")

    def _classify(self, side_effect: Exception) -> str:
        body = _classify_request().model_dump_json().encode()
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/classify",
            "HTTP_AUTHORIZATION": "Bearer test-token",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        statuses: list[str] = []
        adapter = mock.Mock()
        adapter.classify.side_effect = side_effect
        with mock.patch.object(self.wsgi, "build_adapter", return_value=adapter):
            self.wsgi.application(environ, lambda status, _headers: statuses.append(status))
        return statuses[0]

    def test_a_refused_image_is_422(self) -> None:
        from urbanlens_ai.providers.base import ProviderInputRefusedError

        self.assertTrue(self._classify(ProviderInputRefusedError("image too small")).startswith("422 "))

    def test_a_failed_call_is_still_502(self) -> None:
        from urbanlens_ai.providers.base import ProviderError

        self.assertTrue(self._classify(ProviderError("boom")).startswith("502 "))


class InferenceClientTests(SimpleTestCase):
    def _remote(self, status: int, body: dict[str, Any]) -> Exception:
        response = mock.Mock(status_code=status)
        response.json.return_value = body
        with (
            mock.patch("urbanlens.dashboard.services.ai.inference_client.requests.post", return_value=response),
            self.assertRaises(InferenceError) as raised,
        ):
            RemoteInferenceClient("http://ai-inference", "token", timeout_seconds=30.0).classify(_classify_request())
        return raised.exception

    def test_the_remote_client_reads_422_as_a_refusal(self) -> None:
        self.assertIsInstance(self._remote(422, {"error": "provider refused the input"}), InferenceInputRefusedError)

    def test_the_remote_client_reads_502_as_a_failure(self) -> None:
        self.assertNotIsInstance(self._remote(502, {"error": "provider call failed"}), InferenceInputRefusedError)

    def test_the_local_client_passes_a_refusal_on(self) -> None:
        from urbanlens_ai.providers.base import ProviderInputRefusedError

        adapter = mock.Mock()
        adapter.classify.side_effect = ProviderInputRefusedError("image too small")
        with (
            mock.patch("urbanlens_ai.providers.build_adapter", return_value=adapter),
            self.assertRaises(InferenceInputRefusedError),
        ):
            LocalInferenceClient().classify(_classify_request())


def _client(**behaviour: Any) -> Any:
    client = mock.Mock()
    for name, effect in behaviour.items():
        getattr(client, name).side_effect = effect
    return mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client)


class VisionOutcomeTests(TestCase):
    """A refusal is answered with nothing; a call that was not answered is ``None``, not an empty answer."""

    def _logged(self, service: str) -> bool:
        from urbanlens.dashboard.models.api_call_log.model import ApiCallLog

        return ApiCallLog.objects.filter(service=service).latest("created").success

    def test_a_refused_image_classifies_to_no_labels_and_the_call_answered(self) -> None:
        with _client(classify=InferenceInputRefusedError("image too small")):
            self.assertEqual(vision.classify_photo(_JPEG), [])

        self.assertTrue(self._logged(vision.SERVICE_PHOTO_CLASSIFIER))

    def test_a_failed_classification_is_none_and_the_call_failed(self) -> None:
        with _client(classify=InferenceError("ai-inference returned HTTP 502")):
            self.assertIsNone(vision.classify_photo(_JPEG))

        self.assertFalse(self._logged(vision.SERVICE_PHOTO_CLASSIFIER))

    def test_a_classifier_refused_by_the_limiter_is_none(self) -> None:
        refused = RequestCancelledError(vision.SERVICE_PHOTO_CLASSIFIER)
        with mock.patch.object(vision, "api_call_slot", side_effect=refused):
            self.assertIsNone(vision.classify_photo(_JPEG))

    def test_a_refused_image_describes_to_no_keywords_and_the_call_answered(self) -> None:
        with _client(send=InferenceInputRefusedError("image too small")):
            self.assertEqual(vision.describe_photo_keywords(_JPEG), [])

        self.assertTrue(self._logged(vision.SERVICE_AI_PHOTO_KEYWORDS))

    def test_a_failed_description_is_none(self) -> None:
        with _client(send=InferenceError("ai-inference returned HTTP 502")):
            self.assertIsNone(vision.describe_photo_keywords(_JPEG))

    def test_an_answered_description_is_still_parsed(self) -> None:
        answer = InferenceResponse(content=[TextBlock(text="brick, mill")], stop_reason="end_turn", usage=Usage())
        with _client() as patched:
            patched.return_value.send.return_value = answer
            self.assertEqual(vision.describe_photo_keywords(_JPEG), ["brick", "mill"])


class KeywordOutageTests(TestCase):
    """A classifier that did not answer leaves a photo's stored keywords from it alone."""

    def setUp(self) -> None:
        super().setUp()
        user: User = baker.make(User)
        self.image: Image = baker.make(Image, profile=user.profile, _create_files=True)
        patcher = mock.patch(
            "urbanlens.dashboard.plugins.builtin.photo_keywords.require_analysis_jpeg_bytes", return_value=_JPEG
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_classifier_provider_raises_when_the_classifier_did_not_answer(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import ClassifierKeywordProvider
        from urbanlens.dashboard.services.photos.photo_keywords import KeywordSourceUnavailableError

        with (
            mock.patch.object(vision, "classify_photo", return_value=None),
            self.assertRaises(KeywordSourceUnavailableError),
        ):
            ClassifierKeywordProvider().generate(self.image)

    def test_the_vision_provider_raises_when_the_provider_did_not_answer(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import AiVisionKeywordProvider
        from urbanlens.dashboard.services.photos.photo_keywords import KeywordSourceUnavailableError

        with (
            mock.patch.object(vision, "describe_photo_keywords", return_value=None),
            self.assertRaises(KeywordSourceUnavailableError),
        ):
            AiVisionKeywordProvider().generate(self.image)

    def test_an_unanswered_classifier_keeps_the_keywords_it_stored_before(self) -> None:
        from urbanlens.dashboard.models.images import ImageKeyword
        from urbanlens.dashboard.plugins.builtin.photo_keywords import ClassifierKeywordProvider
        from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image

        provider = ClassifierKeywordProvider()
        baker.make("dashboard.ImageKeyword", image=self.image, source=provider.slug, keyword="castle", confidence=0.8)

        with (
            mock.patch(
                "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers", return_value=[provider]
            ),
            mock.patch.object(type(provider), "is_available_for", return_value=True),
            mock.patch.object(vision, "classify_photo", return_value=None),
        ):
            counts = generate_keywords_for_image(self.image.pk)

        self.assertEqual(counts, {})
        self.assertEqual(
            list(ImageKeyword.objects.filter(image=self.image, source=provider.slug).values_list("keyword", flat=True)),
            ["castle"],
        )

    def test_a_refused_image_clears_the_classifier_keywords(self) -> None:
        from urbanlens.dashboard.models.images import ImageKeyword
        from urbanlens.dashboard.plugins.builtin.photo_keywords import ClassifierKeywordProvider
        from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image

        provider = ClassifierKeywordProvider()
        baker.make("dashboard.ImageKeyword", image=self.image, source=provider.slug, keyword="castle", confidence=0.8)

        with (
            mock.patch(
                "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers", return_value=[provider]
            ),
            mock.patch.object(type(provider), "is_available_for", return_value=True),
            mock.patch.object(vision, "classify_photo", return_value=[]),
        ):
            counts = generate_keywords_for_image(self.image.pk)

        self.assertEqual(counts, {provider.slug: 0})
        self.assertFalse(ImageKeyword.objects.filter(image=self.image, source=provider.slug).exists())
