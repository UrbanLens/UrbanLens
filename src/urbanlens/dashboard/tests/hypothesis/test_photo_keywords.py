"""Tests for the photo keyword pipeline: normalization, storage, gating, plugins."""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.images import ImageKeyword
from urbanlens.dashboard.models.images.keyword import MAX_KEYWORD_LENGTH
from urbanlens.dashboard.services.photo_keywords import (
    MAX_KEYWORDS_PER_SOURCE,
    KeywordResult,
    PhotoKeywordProvider,
    generate_keywords_for_image,
    normalize_keywords,
)


class NormalizeKeywordsTests(SimpleTestCase):
    """normalize_keywords cleans, dedupes, and caps provider output."""

    def test_lowercases_and_strips_punctuation(self):
        results = normalize_keywords([KeywordResult(keyword="  Rusty Turbine. ")])
        self.assertEqual(results[0].keyword, "rusty turbine")

    def test_dedupes_keeping_highest_confidence(self):
        results = normalize_keywords(
            [
                KeywordResult(keyword="castle", confidence=0.2),
                KeywordResult(keyword="Castle", confidence=0.9),
            ],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].confidence, 0.9)

    def test_drops_empty_and_overlong(self):
        results = normalize_keywords(
            [
                KeywordResult(keyword="   "),
                KeywordResult(keyword="x" * (MAX_KEYWORD_LENGTH + 1)),
            ],
        )
        self.assertEqual(results, [])

    @settings(max_examples=50, deadline=None)
    @given(st.lists(st.text(max_size=140), max_size=60))
    def test_invariants_hold_for_arbitrary_input(self, raw_keywords: list[str]):
        results = normalize_keywords([KeywordResult(keyword=keyword) for keyword in raw_keywords])
        self.assertLessEqual(len(results), MAX_KEYWORDS_PER_SOURCE)
        seen = set()
        for result in results:
            self.assertEqual(result.keyword, result.keyword.lower())
            self.assertLessEqual(len(result.keyword), MAX_KEYWORD_LENGTH)
            self.assertNotIn(result.keyword, seen)
            seen.add(result.keyword)


class _StaticProvider(PhotoKeywordProvider):
    """Provider returning fixed keywords, for pipeline tests."""

    slug = "test_static"
    label = "Static"

    def __init__(self, keywords=None, available=True):
        self._keywords = keywords or [KeywordResult("Bridge"), KeywordResult("river", confidence=0.8)]
        self._available = available

    def is_available_for(self, image):
        return self._available

    def generate(self, image):
        return list(self._keywords)


class _BrokenProvider(PhotoKeywordProvider):
    """Provider that always raises, to prove provider isolation."""

    slug = "test_broken"
    label = "Broken"

    def generate(self, image):
        raise RuntimeError("provider exploded")


class GenerateKeywordsPipelineTests(TestCase):
    """generate_keywords_for_image stores per-source rows and honors settings."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.image = baker.make("dashboard.Image", profile=self.profile, _create_files=True)

    def _run_with(self, providers):
        with patch("urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers", return_value=providers):
            return generate_keywords_for_image(self.image.pk)

    def test_stores_keywords_attributed_to_provider(self):
        counts = self._run_with([_StaticProvider()])
        self.assertEqual(counts, {"test_static": 2})
        rows = ImageKeyword.objects.filter(image=self.image, source="test_static")
        self.assertEqual({row.keyword for row in rows}, {"bridge", "river"})

    def test_multiple_providers_store_separately(self):
        class _SecondProvider(_StaticProvider):
            slug = "test_second"

        self._run_with([_StaticProvider(), _SecondProvider(keywords=[KeywordResult("tunnel")])])
        self.assertEqual(ImageKeyword.objects.filter(image=self.image, source="test_static").count(), 2)
        self.assertEqual(ImageKeyword.objects.filter(image=self.image, source="test_second").count(), 1)

    def test_rerun_replaces_own_rows_only(self):
        self._run_with([_StaticProvider()])
        baker.make("dashboard.ImageKeyword", image=self.image, source="other_plugin", keyword="untouched")
        self._run_with([_StaticProvider(keywords=[KeywordResult("fresh")])])
        self.assertEqual({row.keyword for row in ImageKeyword.objects.filter(image=self.image, source="test_static")}, {"fresh"})
        self.assertTrue(ImageKeyword.objects.filter(image=self.image, source="other_plugin", keyword="untouched").exists())

    def test_user_setting_disables_generation(self):
        self.profile.generate_photo_keywords = False
        self.profile.save(update_fields=["generate_photo_keywords", "updated"])
        counts = self._run_with([_StaticProvider()])
        self.assertEqual(counts, {})
        self.assertFalse(ImageKeyword.objects.filter(image=self.image).exists())

    def test_unavailable_provider_is_skipped(self):
        counts = self._run_with([_StaticProvider(available=False)])
        self.assertEqual(counts, {})

    def test_broken_provider_does_not_break_others(self):
        counts = self._run_with([_BrokenProvider(), _StaticProvider()])
        self.assertEqual(counts, {"test_static": 2})

    def test_missing_image_is_a_noop(self):
        image_id = self.image.pk
        self.image.delete()
        with patch("urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers", return_value=[_StaticProvider()]):
            self.assertEqual(generate_keywords_for_image(image_id), {})


class AiVisionGatingTests(TestCase):
    """The AI vision provider requires the AI photo processing feature and toggles."""

    def setUp(self):
        from urbanlens.dashboard.plugins.builtin.photo_keywords import AiVisionKeywordProvider

        self.provider = AiVisionKeywordProvider()
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.image = baker.make("dashboard.Image", profile=self.profile, _create_files=True)

    def test_unavailable_without_subscription_feature(self):
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=False):
            self.assertFalse(self.provider.is_available_for(self.image))

    def test_available_with_feature_and_toggles(self):
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=True):
            self.assertTrue(self.provider.is_available_for(self.image))

    def test_unavailable_when_profile_ai_disabled(self):
        self.profile.ai_enabled = False
        self.profile.save(update_fields=["ai_enabled", "updated"])
        self.image.refresh_from_db()
        with patch("urbanlens.dashboard.models.subscriptions.user_has_feature", return_value=True):
            self.assertFalse(self.provider.is_available_for(self.image))

    def test_unavailable_without_profile(self):
        self.image.profile = None
        self.assertFalse(self.provider.is_available_for(self.image))


class MetadataKeywordHelpersTests(SimpleTestCase):
    """XMP subject extraction handles the common packet shapes."""

    def test_bag_of_li_entries(self):
        from urbanlens.dashboard.plugins.builtin.photo_keywords import _xmp_subjects

        xmp = {"xmpmeta": {"RDF": {"Description": {"subject": {"Bag": {"li": ["decay", "brick"]}}}}}}
        self.assertEqual(_xmp_subjects(xmp), ["decay", "brick"])

    def test_flat_string_subject(self):
        from urbanlens.dashboard.plugins.builtin.photo_keywords import _xmp_subjects

        xmp = {"Description": {"subject": "graffiti"}}
        self.assertEqual(_xmp_subjects(xmp), ["graffiti"])

    def test_no_subject_yields_empty(self):
        from urbanlens.dashboard.plugins.builtin.photo_keywords import _xmp_subjects

        self.assertEqual(_xmp_subjects({"Description": {"title": "x"}}), [])


class KeywordParsingTests(SimpleTestCase):
    """Vision-response parsing splits comma/newline keyword lists."""

    def test_parse_keyword_text(self):
        from urbanlens.dashboard.services.ai.vision import _parse_keyword_text

        text = "abandoned factory, broken windows\n graffiti,  rust, "
        self.assertEqual(_parse_keyword_text(text), ["abandoned factory", "broken windows", "graffiti", "rust"])
