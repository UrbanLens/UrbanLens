"""Vision and image classification go through ai-inference, not a provider SDK.

The vision migration's actual claim, in four parts:

1. ``services/ai/vision.py`` builds no provider client and reads no provider
   credential - it hands an :class:`ImagePart` to the inference client like
   every other AI feature (:class:`VisionRoutingTests`,
   :class:`VisionSourceTests`).
2. The wire schema carries an image as inline base64, never a URL the
   inference tier would have to fetch, and every adapter translates it into
   its own provider's shape (:class:`ImageWireShapeTests`).
3. ``policy.py`` bounds what a caller may send: image size, image count, and
   which providers accept images or offer classification at all
   (:class:`VisionPolicyTests`).
4. Classification is its own call, not a chat completion wearing one
   (:class:`ClassifyTests`).
"""

from __future__ import annotations

import ast
import base64
import pathlib
from unittest import mock

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.services.ai import vision
from urbanlens.dashboard.services.ai.inference_client import (
    ClassificationLabel,
    ClassifyResponse,
    ImagePart,
    InferenceRequest,
    InferenceResponse,
    Message,
    TextBlock,
    TextPart,
    Usage,
)

_VISION_PATH = pathlib.Path(vision.__file__)
_ONE_PIXEL = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class VisionSourceTests(SimpleTestCase):
    """vision.py never touches a provider SDK or credential again."""

    def test_imports_no_provider_sdk(self) -> None:
        # The specific regression: this module used to do `from openai import
        # OpenAI` and build a client with settings.openai_api_key, which is a
        # second provider-key surface on the ordinary worker - the exact thing
        # ai-inference exists to remove.
        tree = ast.parse(_VISION_PATH.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("openai", "anthropic", "requests", "httpx"):
            self.assertNotIn(banned, imported, f"vision.py imports {banned} - provider calls belong in ai-inference")

    def test_reads_no_provider_credential(self) -> None:
        source = _VISION_PATH.read_text(encoding="utf-8")
        for banned in ("openai_api_key", "anthropic_api_key", "cloudflare_ai_api_key", "cloudflare_worker_ai_endpoint"):
            self.assertNotIn(banned, source, f"vision.py reads {banned} - that credential lives only in ai-inference")


class ImageWireShapeTests(SimpleTestCase):
    """An image crosses the wire as inline base64 and reaches each provider's own shape."""

    def _message(self) -> Message:
        return Message(
            role="user",
            content=[TextPart(text="what is this"), ImagePart(data=base64.b64encode(_ONE_PIXEL).decode("ascii"))],
        )

    def test_a_text_only_message_still_round_trips_as_a_bare_string(self) -> None:
        # The migration must not disturb the ordinary path: every existing
        # caller passes content as a plain str.
        message = Message(role="user", content="hello")
        self.assertEqual(message.text, "hello")
        self.assertEqual(message.images, [])
        self.assertEqual([part.text for part in message.parts], ["hello"])

    def test_parts_split_into_text_and_images(self) -> None:
        message = self._message()
        self.assertEqual(message.text, "what is this")
        self.assertEqual(len(message.images), 1)
        self.assertEqual(base64.b64decode(message.images[0].data), _ONE_PIXEL)

    def test_openai_sends_a_data_url_not_a_fetchable_one(self) -> None:
        # A fetchable URL would be a network capability this tier is built to
        # not have; inline bytes keep the egress allowlist meaningful.
        from urbanlens_ai.providers.openai import OpenAIAdapter

        content = OpenAIAdapter._user_content(self._message())
        self.assertIsInstance(content, list)
        image_parts = [part for part in content if part["type"] == "image_url"]
        self.assertEqual(len(image_parts), 1)
        self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_anthropic_sends_a_base64_source_block(self) -> None:
        from urbanlens_ai.providers.anthropic import AnthropicAdapter

        content = AnthropicAdapter._content(self._message())
        self.assertIsInstance(content, list)
        image_blocks = [block for block in content if block["type"] == "image"]
        self.assertEqual(len(image_blocks), 1)
        self.assertEqual(
            image_blocks[0]["source"],
            {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(_ONE_PIXEL).decode("ascii")},
        )

    def test_cloudflare_switches_to_its_flat_image_prompt_payload(self) -> None:
        # Workers AI vision models take {image: [bytes], prompt} rather than a
        # messages array - a real provider difference the adapter absorbs.
        from urbanlens_ai.providers.cloudflare import CloudflareAdapter

        adapter = CloudflareAdapter("key", "https://api.cloudflare.com/client/v4/accounts/x/ai/run")
        request = InferenceRequest(
            provider="cloudflare", model="@cf/llava-hf/llava-1.5-7b-hf", messages=[self._message()], max_tokens=300
        )
        captured: dict = {}

        def _fake_post(model, payload, **kwargs):
            captured["model"] = model
            captured["payload"] = payload
            return {"result": {"description": "a brick mill"}}

        with mock.patch.object(adapter, "_post", side_effect=_fake_post):
            response = adapter.send(request)

        self.assertEqual(captured["payload"]["image"], list(_ONE_PIXEL))
        self.assertIn("what is this", captured["payload"]["prompt"])
        self.assertNotIn("messages", captured["payload"])
        self.assertEqual(response.text, "a brick mill")

    def test_cloudflare_text_only_still_uses_the_messages_payload(self) -> None:
        from urbanlens_ai.providers.cloudflare import CloudflareAdapter

        adapter = CloudflareAdapter("key", "https://api.cloudflare.com/client/v4/accounts/x/ai/run")
        request = InferenceRequest(
            provider="cloudflare",
            model="@cf/meta/llama-3-8b-instruct",
            system="be brief",
            messages=[Message(role="user", content="hi")],
            max_tokens=100,
        )
        captured: dict = {}

        with mock.patch.object(
            adapter,
            "_post",
            side_effect=lambda model, payload, **kw: captured.update(payload) or {"result": {"response": "hello"}},
        ):
            adapter.send(request)

        self.assertIn("messages", captured)
        self.assertNotIn("image", captured)


class VisionPolicyTests(SimpleTestCase):
    """policy.py bounds what a caller may send through the image path."""

    def _request(
        self, *, images: int = 1, data: str | None = None, provider: str = "openai", model: str = "gpt-5-nano"
    ) -> InferenceRequest:
        payload = data if data is not None else base64.b64encode(_ONE_PIXEL).decode("ascii")
        parts = [TextPart(text="describe")] + [ImagePart(data=payload) for _ in range(images)]
        return InferenceRequest(
            provider=provider, model=model, messages=[Message(role="user", content=parts)], max_tokens=300
        )

    def test_a_valid_vision_request_passes(self) -> None:
        from urbanlens_ai.policy import validate_request

        validate_request(self._request())

    def test_an_oversized_image_is_refused(self) -> None:
        from urbanlens_ai.policy import MAX_IMAGE_BYTES, PolicyError, validate_request

        oversized = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
        with self.assertRaises(PolicyError) as ctx:
            validate_request(self._request(data=oversized))
        self.assertIn("downscaled", str(ctx.exception))

    def test_the_size_cap_is_on_decoded_bytes_not_base64_characters(self) -> None:
        # Base64 inflates by ~4/3, so counting characters would let an image a
        # third larger than the cap through.
        from urbanlens_ai.policy import MAX_IMAGE_BYTES, PolicyError, validate_request

        just_over = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
        self.assertLess(MAX_IMAGE_BYTES, len(just_over), "test premise: the base64 string is longer than the byte cap")
        with self.assertRaises(PolicyError):
            validate_request(self._request(data=just_over))

    def test_non_base64_image_data_is_refused(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_request

        with self.assertRaises(PolicyError):
            validate_request(self._request(data="not base64 !!!"))

    def test_too_many_images_are_refused(self) -> None:
        from urbanlens_ai.policy import MAX_IMAGES_PER_REQUEST, PolicyError, validate_request

        with self.assertRaises(PolicyError):
            validate_request(self._request(images=MAX_IMAGES_PER_REQUEST + 1))

    def test_a_text_only_request_skips_the_image_checks_entirely(self) -> None:
        from urbanlens_ai.policy import validate_request

        validate_request(
            InferenceRequest(
                provider="anthropic",
                model="claude-sonnet-5",
                messages=[Message(role="user", content="hi")],
                max_tokens=100,
            )
        )

    def test_classification_is_refused_for_a_provider_that_has_none(self) -> None:
        from urbanlens_ai.policy import PolicyError, validate_classify_request
        from urbanlens_ai.schema import ClassifyRequest

        request = ClassifyRequest(
            provider="openai", model="gpt-5-nano", image=ImagePart(data=base64.b64encode(_ONE_PIXEL).decode("ascii"))
        )
        with self.assertRaises(PolicyError) as ctx:
            validate_classify_request(request)
        self.assertIn("classification", str(ctx.exception))

    def test_a_valid_cloudflare_classify_request_passes(self) -> None:
        from urbanlens_ai.policy import validate_classify_request
        from urbanlens_ai.schema import ClassifyRequest

        validate_classify_request(
            ClassifyRequest(
                provider="cloudflare",
                model="@cf/microsoft/resnet-50",
                image=ImagePart(data=base64.b64encode(_ONE_PIXEL).decode("ascii")),
            )
        )

    def test_an_oversized_classify_image_is_refused(self) -> None:
        from urbanlens_ai.policy import MAX_IMAGE_BYTES, PolicyError, validate_classify_request
        from urbanlens_ai.schema import ClassifyRequest

        oversized = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
        with self.assertRaises(PolicyError):
            validate_classify_request(
                ClassifyRequest(provider="cloudflare", model="@cf/microsoft/resnet-50", image=ImagePart(data=oversized))
            )


class VisionRoutingTests(TestCase):
    """describe_photo_keywords sends one image through the inference client."""

    def _patched(self, response: InferenceResponse):
        client = mock.Mock()
        client.send.return_value = response
        return client, mock.patch(
            "urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client
        )

    def test_the_image_is_sent_inline_with_the_prompt(self) -> None:
        client, patched = self._patched(
            InferenceResponse(content=[TextBlock(text="brick, mill")], stop_reason="end_turn", usage=Usage())
        )
        with patched:
            self.assertEqual(vision.describe_photo_keywords(_ONE_PIXEL), ["brick", "mill"])

        request = client.send.call_args.args[0]
        self.assertEqual(len(request.messages), 1)
        images = request.messages[0].images
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0].data), _ONE_PIXEL)
        self.assertIn("keywords", request.messages[0].text)

    def test_the_site_provider_setting_chooses_the_target(self) -> None:
        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(ai_provider="cloudflare")
        client, patched = self._patched(
            InferenceResponse(content=[TextBlock(text="a")], stop_reason="end_turn", usage=Usage())
        )
        with patched:
            vision.describe_photo_keywords(_ONE_PIXEL)
        self.assertEqual(client.send.call_args.args[0].provider, "cloudflare")

    def test_the_request_this_builds_passes_its_own_policy(self) -> None:
        # The two halves are validated in different processes in production
        # (app builds it, ai-inference checks it), so nothing but a test
        # catches a caller that builds something the service will refuse.
        from urbanlens_ai.policy import validate_request

        settings = SiteSettings.get_current()
        SiteSettings.objects.filter(pk=settings.pk).update(ai_provider="openai")
        client, patched = self._patched(
            InferenceResponse(content=[TextBlock(text="a")], stop_reason="end_turn", usage=Usage())
        )
        with patched:
            vision.describe_photo_keywords(_ONE_PIXEL)

        validate_request(client.send.call_args.args[0])


class ClassifyTests(TestCase):
    """classify_photo takes the separate classify call, not a chat completion."""

    def test_labels_come_back_sorted_and_typed(self) -> None:
        client = mock.Mock()
        client.classify.return_value = ClassifyResponse(
            labels=[ClassificationLabel(label="castle", score=0.9), ClassificationLabel(label="ruin", score=0.4)]
        )
        with mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client):
            labels = vision.classify_photo(_ONE_PIXEL)

        self.assertEqual(labels, [("castle", 0.9), ("ruin", 0.4)])
        client.send.assert_not_called()
        request = client.classify.call_args.args[0]
        self.assertEqual(request.provider, "cloudflare")
        self.assertEqual(base64.b64decode(request.image.data), _ONE_PIXEL)

    def test_a_failure_is_swallowed_into_an_empty_list(self) -> None:
        from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
        from urbanlens.dashboard.services.ai.inference_client import InferenceError

        client = mock.Mock()
        client.classify.side_effect = InferenceError("boom")
        with mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client):
            self.assertEqual(vision.classify_photo(_ONE_PIXEL), [])

        self.assertFalse(ApiCallLog.objects.filter(service=vision.SERVICE_PHOTO_CLASSIFIER).latest("created").success)

    def test_the_cloudflare_adapter_normalizes_and_sorts_raw_labels(self) -> None:
        from urbanlens_ai.providers.cloudflare import CloudflareAdapter
        from urbanlens_ai.schema import ClassifyRequest

        adapter = CloudflareAdapter("key", "https://api.cloudflare.com/client/v4/accounts/x/ai/run")
        raw = {
            "result": [
                {"label": "ruin", "score": 0.2},
                {"label": "castle", "score": 0.8},
                {"label": "", "score": 0.9},
                "junk",
            ]
        }
        request = ClassifyRequest(
            provider="cloudflare",
            model="@cf/microsoft/resnet-50",
            image=ImagePart(data=base64.b64encode(_ONE_PIXEL).decode("ascii")),
        )

        with mock.patch.object(adapter, "_post", return_value=raw):
            response = adapter.classify(request)

        # Blank labels and non-dict entries dropped; highest confidence first.
        self.assertEqual([(item.label, item.score) for item in response.labels], [("castle", 0.8), ("ruin", 0.2)])

    def test_an_adapter_without_a_classifier_raises_rather_than_attribute_errors(self) -> None:
        from urbanlens_ai.providers.base import ProviderError
        from urbanlens_ai.providers.openai import OpenAIAdapter
        from urbanlens_ai.schema import ClassifyRequest

        adapter = OpenAIAdapter("key")
        request = ClassifyRequest(
            provider="cloudflare",
            model="@cf/microsoft/resnet-50",
            image=ImagePart(data=base64.b64encode(_ONE_PIXEL).decode("ascii")),
        )
        with self.assertRaises(ProviderError):
            adapter.classify(request)
