"""A keyword source that did not answer leaves a photo's keywords as they were (P322).

Ollama returned ``[]`` for a refused connection, a timeout, a 5xx or an error body, and
``generate_keywords_for_image`` stored that as the photo's answer: it deleted the Ollama keywords the
photo had and stored none. A photo whose analysis copy was missing or unreadable did the same for all
three image providers. Each now raises ``KeywordSourceUnavailableError``, and only an answer replaces
what was stored.
"""

from __future__ import annotations

import io
from unittest import mock

from django.core.files.base import ContentFile
from model_bakery import baker
from PIL import Image as PILImage
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.images.keyword import ImageKeyword
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.photos.photo_keywords import (
    AnalysisCopyUnavailableError,
    KeywordSourceUnavailableError,
    generate_keywords_for_image,
)

_OLLAMA_URL = "http://ollama.test:11434"
_SETTINGS = "urbanlens.UrbanLens.settings.app.settings"
_REGISTRY = "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers"


def _jpeg(width: int = 64, height: int = 48) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 60, 30)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _response(body: object, *, status: int = 200, json_error: bool = False) -> requests.Response:
    """A real ``requests.Response``, so ``raise_for_status`` and ``json`` behave as they do on the wire."""
    response = requests.Response()
    response.status_code = status
    response.url = f"{_OLLAMA_URL}/api/generate"
    response.headers["Content-Type"] = "application/json"
    if json_error:
        response._content = b"<html>bad gateway</html>"
    else:
        import json

        response._content = json.dumps(body).encode()
    return response


def _gateway():
    from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway

    return OllamaGateway(base_url=_OLLAMA_URL, model="llava")


class OllamaAnswerTests(TestCase):
    """``OllamaGateway.describe_photo_keywords``: keywords for an answer, None when none came."""

    def _ask(self, outcome: requests.Response | Exception) -> list[str] | None:
        side_effect = outcome if isinstance(outcome, Exception) else None
        with mock.patch("requests.Session.request", return_value=outcome, side_effect=side_effect):
            return _gateway().describe_photo_keywords(_jpeg())

    def test_an_answer_is_its_keywords(self) -> None:
        self.assertEqual(
            self._ask(_response({"response": "brick, mill", "done": True, "done_reason": "stop"})), ["brick", "mill"]
        )

    def test_an_answer_with_nothing_in_it_is_an_empty_answer(self) -> None:
        # The model answered and named nothing: that is what it sees, not an outage.
        self.assertEqual(self._ask(_response({"response": "", "done": True, "done_reason": "stop"})), [])
        self.assertEqual(self._ask(_response({"response": " , ;", "done": True})), [])

    def test_a_refused_connection_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(requests.ConnectionError("connection refused")))

    def test_a_timeout_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(requests.Timeout("read timed out")))

    def test_a_server_error_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(_response({"error": "the model failed to generate a response"}, status=500)))

    def test_a_missing_model_is_no_answer(self) -> None:
        # Ollama's 404 for a model nobody pulled: a deployment fault, not a photo with no keywords.
        self.assertIsNone(self._ask(_response({"error": "model 'llava' not found"}, status=404)))

    def test_an_error_body_with_a_200_is_no_answer(self) -> None:
        # Ollama keeps the status it has already sent when an error comes after the response started.
        self.assertIsNone(self._ask(_response({"error": "unexpected EOF"})))

    def test_a_body_that_is_not_json_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(_response(None, json_error=True)))

    def test_a_body_that_is_not_an_object_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(_response(["brick"])))

    def test_a_body_without_a_response_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(_response({"done": True})))
        self.assertIsNone(self._ask(_response({"response": None, "done": True})))

    def test_an_unfinished_generation_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(_response({"response": "bri", "done": False})))

    def test_a_generation_cut_off_before_it_said_anything_is_no_answer(self) -> None:
        # The output budget ran out (a thinking model can spend it all) before any text: the model never answered.
        self.assertIsNone(self._ask(_response({"response": "", "done": True, "done_reason": "length"})))
        self.assertIsNone(self._ask(_response({"response": " \n", "done": True, "done_reason": "length"})))

    def test_a_cut_off_generation_that_said_something_is_an_answer(self) -> None:
        self.assertEqual(
            self._ask(_response({"response": "brick, mi", "done": True, "done_reason": "length"})), ["brick", "mi"]
        )

    def test_a_done_reason_of_another_type_is_no_answer_and_does_not_raise(self) -> None:
        self.assertIsNone(self._ask(_response({"response": "brick", "done": True, "done_reason": ["load"]})))
        self.assertIsNone(self._ask(_response({"response": "brick", "done": True, "done_reason": {"a": 1}})))

    def test_a_call_refused_before_it_was_sent_is_no_answer(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        with (
            mock.patch(
                "urbanlens.dashboard.services.core.rate_limiter._reserve_call",
                side_effect=RateLimitExceededError("ollama"),
            ),
            mock.patch("requests.Session.request") as request,
        ):
            self.assertIsNone(_gateway().describe_photo_keywords(_jpeg()))
        request.assert_not_called()

    def test_a_load_without_a_generation_is_no_answer(self) -> None:
        self.assertIsNone(self._ask(_response({"response": "", "done": True, "done_reason": "load"})))
        self.assertIsNone(self._ask(_response({"response": "", "done": True, "done_reason": "unload"})))

    def test_no_server_configured_is_no_answer(self) -> None:
        from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway

        with mock.patch("requests.Session.request") as request:
            self.assertIsNone(OllamaGateway(base_url="", model="llava").describe_photo_keywords(_jpeg()))
        request.assert_not_called()


def _photo(**profile_fields: object) -> Image:
    profile = _make_profile(**profile_fields)
    image = baker.make(Image, media_type=MediaKind.PHOTO, profile=profile)
    image.image.save("original.jpg", ContentFile(_jpeg(1200, 900)), save=True)
    return image


def _with_analysis_copy(image: Image) -> Image:
    image.analysis_thumbnail.save("original-analysis.jpg", ContentFile(_jpeg()), save=True)
    return image


class OllamaProviderTests(TestCase):
    """The Ollama keyword provider raises when its source did not answer."""

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch(f"{_SETTINGS}.ollama_base_url", _OLLAMA_URL))
        self.image = _with_analysis_copy(_photo())

    def _provider(self):
        from urbanlens.dashboard.plugins.builtin.ollama import OllamaVisionKeywordProvider

        return OllamaVisionKeywordProvider()

    def test_no_answer_raises(self) -> None:
        with (
            mock.patch(
                "urbanlens.dashboard.services.apis.ai.ollama.OllamaGateway.describe_photo_keywords", return_value=None
            ),
            self.assertRaises(KeywordSourceUnavailableError),
        ):
            self._provider().generate(self.image)

    def test_an_empty_answer_is_no_keywords(self) -> None:
        with mock.patch(
            "urbanlens.dashboard.services.apis.ai.ollama.OllamaGateway.describe_photo_keywords", return_value=[]
        ):
            self.assertEqual(self._provider().generate(self.image), [])

    def test_a_photo_without_an_analysis_copy_is_not_asked_about(self) -> None:
        bare = _photo()
        with (
            mock.patch("urbanlens.dashboard.services.apis.ai.ollama.OllamaGateway.describe_photo_keywords") as describe,
            self.assertRaises(AnalysisCopyUnavailableError),
        ):
            self._provider().generate(bare)
        describe.assert_not_called()


class AnalysisCopyUnavailableTests(TestCase):
    """Every provider that reads the analysis copy raises when it is missing or unreadable, rather than answering ``[]``."""

    def _providers(self):
        from urbanlens.dashboard.plugins.builtin.ollama import OllamaVisionKeywordProvider
        from urbanlens.dashboard.plugins.builtin.photo_keywords import (
            AiVisionKeywordProvider,
            ClassifierKeywordProvider,
        )

        return [AiVisionKeywordProvider(), ClassifierKeywordProvider(), OllamaVisionKeywordProvider()]

    def test_a_missing_copy_raises_for_every_image_provider(self) -> None:
        image = _photo()
        for provider in self._providers():
            with self.subTest(provider=provider.slug), self.assertRaises(AnalysisCopyUnavailableError):
                provider.generate(image)

    def test_an_unreadable_copy_raises_for_every_image_provider(self) -> None:
        image = _with_analysis_copy(_photo())
        with mock.patch.object(type(image.analysis_thumbnail), "open", side_effect=OSError("storage is down")):
            for provider in self._providers():
                with self.subTest(provider=provider.slug), self.assertRaises(AnalysisCopyUnavailableError):
                    provider.generate(image)

    def test_the_error_is_a_source_unavailable_error(self) -> None:
        # The pipeline's existing ``except KeywordSourceUnavailableError`` is what keeps the stored keywords.
        self.assertTrue(issubclass(AnalysisCopyUnavailableError, KeywordSourceUnavailableError))


class OllamaOutageKeepsKeywordsTests(TestCase):
    """End to end: an Ollama outage leaves a photo's Ollama keywords in place; an answer replaces them."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.plugins.builtin.ollama import OllamaVisionKeywordProvider

        self.enterContext(mock.patch(f"{_SETTINGS}.ollama_base_url", _OLLAMA_URL))
        self.enterContext(mock.patch(_REGISTRY, return_value=[OllamaVisionKeywordProvider()]))
        self.image = _with_analysis_copy(_photo())
        ImageKeyword.objects.create(image=self.image, source="photo_keywords_ollama", keyword="factory")
        ImageKeyword.objects.create(image=self.image, source="photo_keywords_ollama", keyword="brick")

    def _stored(self) -> set[str]:
        return set(
            ImageKeyword.objects.filter(image=self.image, source="photo_keywords_ollama").values_list(
                "keyword", flat=True
            )
        )

    def _run(self, outcome: requests.Response | Exception) -> dict[str, int]:
        side_effect = outcome if isinstance(outcome, Exception) else None
        with mock.patch("requests.Session.request", return_value=outcome, side_effect=side_effect):
            return generate_keywords_for_image(self.image.pk)

    def test_a_refused_connection_keeps_the_keywords(self) -> None:
        counts = self._run(requests.ConnectionError("connection refused"))
        self.assertEqual(self._stored(), {"factory", "brick"})
        self.assertNotIn("photo_keywords_ollama", counts)

    def test_a_server_error_keeps_the_keywords(self) -> None:
        self._run(_response({"error": "out of memory"}, status=500))
        self.assertEqual(self._stored(), {"factory", "brick"})

    def test_an_error_body_keeps_the_keywords(self) -> None:
        self._run(_response({"error": "unexpected EOF"}))
        self.assertEqual(self._stored(), {"factory", "brick"})

    def test_an_answer_replaces_the_keywords(self) -> None:
        counts = self._run(_response({"response": "mill, smokestack", "done": True, "done_reason": "stop"}))
        self.assertEqual(self._stored(), {"mill", "smokestack"})
        self.assertEqual(counts["photo_keywords_ollama"], 2)

    def test_an_empty_answer_clears_the_keywords(self) -> None:
        self._run(_response({"response": "", "done": True, "done_reason": "stop"}))
        self.assertEqual(self._stored(), set())


class UnreadableCopyKeepsKeywordsTests(TestCase):
    """A storage failure reading the analysis copy on a re-run keeps every image provider's keywords."""

    def test_the_hosted_providers_keep_their_keywords(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import (
            AiVisionKeywordProvider,
            ClassifierKeywordProvider,
        )

        image = _with_analysis_copy(_photo())
        ImageKeyword.objects.create(image=image, source="photo_keywords_ai_vision", keyword="stairwell")
        ImageKeyword.objects.create(image=image, source="photo_keywords_classifier", keyword="mill")
        providers = [AiVisionKeywordProvider(), ClassifierKeywordProvider()]
        with (
            mock.patch(_REGISTRY, return_value=providers),
            mock.patch.object(AiVisionKeywordProvider, "is_available_for", return_value=True),
            mock.patch.object(ClassifierKeywordProvider, "is_available_for", return_value=True),
            mock.patch.object(type(image.analysis_thumbnail), "open", side_effect=OSError("storage is down")),
            mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client") as client,
        ):
            counts = generate_keywords_for_image(image.pk)

        client.assert_not_called()
        self.assertEqual(counts, {})
        self.assertEqual(
            set(ImageKeyword.objects.filter(image=image).values_list("source", "keyword")),
            {("photo_keywords_ai_vision", "stairwell"), ("photo_keywords_classifier", "mill")},
        )


class EveryImageProviderRaisesTests(SimpleTestCase):
    """No built-in image provider turns "no answer" into ``[]`` (P322)."""

    def test_no_image_provider_returns_empty_for_a_missing_copy(self) -> None:
        import ast
        import pathlib

        from urbanlens.dashboard.plugins.builtin import ollama, photo_keywords

        for module in (ollama, photo_keywords):
            tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "generate":
                    called = {
                        child.func.id
                        for child in ast.walk(node)
                        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
                    }
                    with self.subTest(module=module.__name__, line=node.lineno):
                        self.assertNotIn(
                            "analysis_jpeg_bytes",
                            called,
                            "use require_analysis_jpeg_bytes, which raises instead of returning None",
                        )
