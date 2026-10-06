"""An analysis copy too small for Cloudflare's classifier is refused before it is sent, and the refusal is counted (P324).

ResNet-50 on Workers AI answers an image under 4x4 pixels with HTTP 400, code 3011, "image too small" (P320), and that
call still costs a request. The analysis copy is downscaled to 512 px on its long side, so a tiny original or a wide
panorama (2000x10 becomes 512x3) is always one.
"""

from __future__ import annotations

import io
from unittest import mock

from django.core.files.base import ContentFile
from model_bakery import baker
from PIL import Image as PILImage

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.api_call_log.model import ApiCallLog
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.ai import vision
from urbanlens.dashboard.services.ai.inference_client import (
    ClassificationLabel,
    ClassifyResponse,
    InferenceResponse,
    TextBlock,
    Usage,
)

CLIENT_PATH = "urbanlens.dashboard.services.ai.inference_client.get_inference_client"


def _jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 60, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height)).save(buffer, format="PNG")
    return buffer.getvalue()


def _client() -> mock.Mock:
    client = mock.Mock()
    client.classify.return_value = ClassifyResponse(labels=[ClassificationLabel(label="mill", score=0.9)])
    client.send.return_value = InferenceResponse(
        content=[TextBlock(text="brick, mill")], stop_reason="end_turn", usage=Usage()
    )
    return client


class ClassifyPhotoTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.inference = _client()
        self.enterContext(mock.patch(CLIENT_PATH, return_value=self.inference))

    def _rows(self) -> list[ApiCallLog]:
        return list(ApiCallLog.objects.filter(service=vision.SERVICE_PHOTO_CLASSIFIER))

    def assert_refused_unsent(self, image_bytes: bytes) -> None:
        self.assertEqual(vision.classify_photo(image_bytes), [])

        self.inference.classify.assert_not_called()
        rows = self._rows()
        self.assertEqual(len(rows), 1, "a refusal writes exactly one row")
        self.assertTrue(rows[0].was_rejected_input)
        self.assertEqual(rows[0].endpoint, "rejected:out_of_range")
        self.assertEqual(ApiCallLog.objects.for_service(vision.SERVICE_PHOTO_CLASSIFIER).billable().count(), 0)

    def assert_sent(self, image_bytes: bytes) -> None:
        self.assertEqual(vision.classify_photo(image_bytes), [("mill", 0.9)])

        self.inference.classify.assert_called_once()
        self.assertEqual([row.was_rejected_input for row in self._rows()], [False])

    def test_a_panorama_copy_three_pixels_high_is_refused_without_a_call(self) -> None:
        self.assert_refused_unsent(_jpeg(512, 3))

    def test_a_copy_three_pixels_wide_is_refused_without_a_call(self) -> None:
        self.assert_refused_unsent(_jpeg(3, 512))

    def test_a_one_pixel_copy_is_refused_without_a_call(self) -> None:
        self.assert_refused_unsent(_jpeg(1, 1))

    def test_a_four_by_four_copy_is_sent(self) -> None:
        self.assert_sent(_jpeg(4, 4))

    def test_bytes_whose_size_cannot_be_read_are_left_to_the_classifier(self) -> None:
        for name, image_bytes in {
            "not an image": b"not-a-jpeg",
            "a JPEG cut short in its header": _jpeg(1, 1)[:40],
            "a format the header reader does not read": _png(1, 1),
        }.items():
            with self.subTest(name):
                ApiCallLog.objects.all().delete()
                self.inference.classify.reset_mock()
                self.assert_sent(image_bytes)

    def test_the_vision_description_is_not_held_to_the_classifiers_minimum(self) -> None:
        """Only ResNet-50 is known to refuse a tiny image; the vision models are still asked."""
        self.assertEqual(vision.describe_photo_keywords(_jpeg(1, 1)), ["brick", "mill"])

        self.inference.send.assert_called_once()
        self.assertFalse(ApiCallLog.objects.filter(was_rejected_input=True).exists())


class ClassifierKeywordProviderTests(TestCase):
    """The keyword pipeline, from a panorama's original through the sandbox writer's copy to the stored keywords."""

    def test_a_panorama_whose_copy_is_too_small_stores_no_classifier_keywords_and_spends_no_call(self) -> None:
        from urbanlens.dashboard.models.images import ImageKeyword
        from urbanlens.dashboard.plugins.builtin.photo_keywords import ClassifierKeywordProvider
        from urbanlens.dashboard.services.media.images import write_image_analysis_thumbnail
        from urbanlens.dashboard.services.photos.photo_keywords import generate_keywords_for_image

        image = baker.make(Image, media_type=MediaKind.PHOTO, profile=_make_profile(generate_photo_keywords=True))
        image.image.save("panorama.jpg", ContentFile(_jpeg(2000, 10)), save=True)
        self.assertTrue(write_image_analysis_thumbnail(image))
        image.save(update_fields=["analysis_thumbnail"])
        with image.analysis_thumbnail.open("rb") as stored, PILImage.open(stored) as copy:
            self.assertEqual(copy.size, (512, 3), "the premise: the writer's copy of a 2000x10 panorama")

        provider = ClassifierKeywordProvider()
        baker.make("dashboard.ImageKeyword", image=image, source=provider.slug, keyword="castle", confidence=0.8)
        client = _client()
        with (
            mock.patch(
                "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers", return_value=[provider]
            ),
            mock.patch.object(type(provider), "is_available_for", return_value=True),
            mock.patch(CLIENT_PATH, return_value=client),
        ):
            counts = generate_keywords_for_image(image.pk)

        self.assertEqual(counts, {provider.slug: 0})
        self.assertFalse(ImageKeyword.objects.filter(image=image, source=provider.slug).exists())
        client.classify.assert_not_called()
        rows = ApiCallLog.objects.filter(service=vision.SERVICE_PHOTO_CLASSIFIER)
        self.assertEqual([row.was_rejected_input for row in rows], [True])
