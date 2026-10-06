"""A keyword source that did not answer for a photo is asked again, boundedly, once it can answer (P323).

``generate_keywords_for_image`` records an ``ImageKeywordRetry`` row for each (photo, source) that did not answer, and
the ``keyword-retry-sweep`` beat entry asks that source again when the row comes due: a capped batch per run, a
source at a time, stopping a source at its first failure or refusal, backing off from a source that keeps failing,
never asking a source this environment does not call, and giving a photo up after a bounded number of failures that
were its own.
"""

from __future__ import annotations

from datetime import timedelta
import io
import json
from unittest import mock

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone
from model_bakery import baker
from PIL import Image as PILImage
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.baker_recipes import _make_profile
from urbanlens.dashboard.models.images.keyword import ImageKeyword, ImageKeywordRetry
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.services.photos import keyword_retry
from urbanlens.dashboard.services.photos.photo_keywords import (
    KeywordOutcome,
    KeywordResult,
    PhotoKeywordProvider,
    generate_keywords_for_image,
)

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "keyword-retry-tests"}}
_OLLAMA_URL = "http://ollama.test:11434"
_SETTINGS = "urbanlens.UrbanLens.settings.app.settings"
_REGISTRY = "urbanlens.dashboard.plugins.registry.plugin_registry.photo_keyword_providers"
_OLLAMA = "photo_keywords_ollama"


def _jpeg(width: int = 64, height: int = 48) -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", (width, height), (90, 60, 30)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def _answer(text: str = "mill, smokestack") -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = f"{_OLLAMA_URL}/api/generate"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps({"response": text, "done": True, "done_reason": "stop"}).encode()
    return response


def _photo(*, analysis_copy: bool = True, **profile_fields: object) -> Image:
    profile = _make_profile(**profile_fields)
    image = baker.make(Image, media_type=MediaKind.PHOTO, profile=profile)
    image.image.save("original.jpg", ContentFile(_jpeg(1200, 900)), save=True)
    if analysis_copy:
        image.analysis_thumbnail.save("original-analysis.jpg", ContentFile(_jpeg()), save=True)
    return image


def _due(
    image: Image, source: str = _OLLAMA, *, attempts: int = 0, ago: timedelta = timedelta(minutes=1)
) -> ImageKeywordRetry:
    return ImageKeywordRetry.objects.create(
        image=image, source=source, attempts=attempts, retry_at=timezone.now() - ago
    )


def _row(image: Image, source: str = _OLLAMA) -> ImageKeywordRetry | None:
    return ImageKeywordRetry.objects.filter(image=image, source=source).first()


def _stored(image: Image, source: str = _OLLAMA) -> set[str]:
    return set(ImageKeyword.objects.filter(image=image, source=source).values_list("keyword", flat=True))


class _OllamaCase(TestCase):
    """The real Ollama provider, its HTTP replaced; a cache of its own so backoffs do not leak between tests."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.plugins.builtin.ollama import OllamaVisionKeywordProvider
        from urbanlens.dashboard.services.core import provider_health

        self.enterContext(override_settings(CACHES=_LOCMEM))
        cache.clear()
        provider_health.forget_snapshot()
        self.addCleanup(provider_health.forget_snapshot)
        self.enterContext(mock.patch(f"{_SETTINGS}.ollama_base_url", _OLLAMA_URL))
        self.provider = OllamaVisionKeywordProvider()
        self.enterContext(mock.patch(_REGISTRY, return_value=[self.provider]))

    def http(self, *outcomes: requests.Response | Exception) -> mock.MagicMock:
        """Answer each Ollama request with the next outcome; the mock counts the requests sent."""
        return self.enterContext(mock.patch("requests.Session.request", side_effect=list(outcomes)))


@override_settings(CACHES=_LOCMEM)
class RecordingTests(_OllamaCase):
    """``generate_keywords_for_image`` records what a source did not answer, and forgets it once it answers."""

    def test_a_source_that_did_not_answer_is_recorded_without_counting_against_the_photo(self) -> None:
        image = _photo()
        self.http(requests.ConnectionError("connection refused"))
        before = timezone.now()

        generate_keywords_for_image(image.pk)

        row = _row(image)
        self.assertIsNotNone(row)
        self.assertEqual(row.attempts, 0)
        self.assertGreaterEqual(row.retry_at, before + keyword_retry.RETRY_DELAYS[0])

    def test_an_answer_forgets_the_retry(self) -> None:
        image = _photo()
        _due(image, attempts=2)
        self.http(_answer())

        generate_keywords_for_image(image.pk)

        self.assertIsNone(_row(image))
        self.assertEqual(_stored(image), {"mill", "smokestack"})

    def test_an_empty_answer_is_an_answer(self) -> None:
        image = _photo()
        _due(image)
        self.http(_answer(""))

        generate_keywords_for_image(image.pk)

        self.assertIsNone(_row(image))

    def test_a_refusal_for_now_is_recorded_and_nothing_was_sent(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        image = _photo()
        with mock.patch(
            "urbanlens.dashboard.services.core.rate_limiter._reserve_call", side_effect=RateLimitExceededError("ollama")
        ):
            request = self.http()
            generate_keywords_for_image(image.pk)

        request.assert_not_called()
        self.assertEqual(_row(image).attempts, 0)

    def test_a_switched_off_service_records_nothing(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import ServiceDisabledError

        image = _photo()
        with mock.patch(
            "urbanlens.dashboard.services.core.rate_limiter._reserve_call", side_effect=ServiceDisabledError("ollama")
        ):
            generate_keywords_for_image(image.pk)

        self.assertIsNone(_row(image))

    def test_a_provider_not_available_for_the_photo_records_nothing(self) -> None:
        image = _photo(ai_enabled=False)
        request = self.http()

        generate_keywords_for_image(image.pk)

        request.assert_not_called()
        self.assertIsNone(_row(image))

    def test_a_photo_without_an_analysis_copy_is_left_to_the_backfill(self) -> None:
        # generate_image_analysis_thumbnails writes the copy and enqueues keywording itself.
        image = _photo(analysis_copy=False)
        request = self.http()

        generate_keywords_for_image(image.pk)

        request.assert_not_called()
        self.assertIsNone(_row(image))

    def test_an_unreadable_analysis_copy_is_recorded_without_counting(self) -> None:
        image = _photo()
        with mock.patch.object(type(image.analysis_thumbnail), "open", side_effect=OSError("storage is down")):
            generate_keywords_for_image(image.pk)

        self.assertEqual(_row(image).attempts, 0)

    def test_a_soft_time_limit_is_not_taken_for_the_source_failing(self) -> None:
        from celery.exceptions import SoftTimeLimitExceeded

        image = _photo()
        self.http(SoftTimeLimitExceeded())

        with self.assertRaises(SoftTimeLimitExceeded):
            generate_keywords_for_image(image.pk)
        self.assertIsNone(_row(image))

    def test_a_given_up_photo_stays_given_up_after_another_failure(self) -> None:
        image = _photo()
        ImageKeywordRetry.objects.create(
            image=image, source=_OLLAMA, attempts=keyword_retry.MAX_ATTEMPTS, retry_at=None
        )
        self.http(requests.ConnectionError("connection refused"))

        generate_keywords_for_image(image.pk)

        row = _row(image)
        self.assertIsNone(row.retry_at)
        self.assertEqual(row.attempts, keyword_retry.MAX_ATTEMPTS)


@override_settings(CACHES=_LOCMEM)
class SweepTests(_OllamaCase):
    """The sweep asks a source again about the photos it did not answer for, and only those."""

    def test_a_due_photo_is_asked_again_and_its_keywords_stored(self) -> None:
        image = _photo()
        _due(image)
        request = self.http(_answer())

        outcomes = keyword_retry.sweep()

        self.assertEqual(request.call_count, 1)
        self.assertEqual(_stored(image), {"mill", "smokestack"})
        self.assertIsNone(_row(image))
        self.assertEqual(outcomes[KeywordOutcome.ANSWERED], 1)

    def test_only_the_source_that_did_not_answer_is_asked(self) -> None:
        class _Other(PhotoKeywordProvider):
            slug = "test_other"
            generate = mock.Mock(return_value=[KeywordResult("other")])

        image = _photo()
        _due(image)
        self.http(_answer())
        with mock.patch(_REGISTRY, return_value=[_Other(), self.provider]):
            keyword_retry.sweep()

        _Other.generate.assert_not_called()

    def test_a_photo_not_yet_due_is_left_alone(self) -> None:
        image = _photo()
        ImageKeywordRetry.objects.create(image=image, source=_OLLAMA, retry_at=timezone.now() + timedelta(minutes=5))
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()

    def test_a_given_up_photo_is_never_asked_again(self) -> None:
        image = _photo()
        ImageKeywordRetry.objects.create(
            image=image, source=_OLLAMA, attempts=keyword_retry.MAX_ATTEMPTS, retry_at=None
        )
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()

    def test_one_run_asks_about_at_most_its_batch(self) -> None:
        photos = [_photo() for _ in range(5)]
        for index, image in enumerate(photos):
            _due(image, ago=timedelta(minutes=10 - index))
        request = self.http(*[_answer() for _ in photos])

        keyword_retry.sweep(limit=3)

        self.assertEqual(request.call_count, 3)
        # The longest-waiting first.
        self.assertEqual([image.pk for image in photos if _row(image) is None], [image.pk for image in photos[:3]])

    def test_running_twice_asks_about_each_photo_once(self) -> None:
        photos = [_photo() for _ in range(2)]
        for image in photos:
            _due(image)
        request = self.http(_answer(), _answer())

        keyword_retry.sweep()
        keyword_retry.sweep()

        self.assertEqual(request.call_count, 2)

    def test_a_photo_that_turned_keywords_off_is_dropped_without_a_call(self) -> None:
        image = _photo(generate_photo_keywords=False)
        _due(image)
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()
        self.assertIsNone(_row(image))

    def test_a_photo_the_provider_no_longer_runs_for_is_dropped_without_a_call(self) -> None:
        image = _photo(ai_enabled=False)
        _due(image)
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()
        self.assertIsNone(_row(image))

    def test_rows_for_a_source_no_plugin_provides_are_left_alone(self) -> None:
        image = _photo()
        row = _due(image, source="photo_keywords_uninstalled")
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()
        self.assertEqual(ImageKeywordRetry.objects.get(pk=row.pk).retry_at, row.retry_at)


_TIME = "urbanlens.dashboard.services.photos.keyword_retry.time.time"


def _down(count: int) -> list[Exception]:
    return [requests.ConnectionError("connection refused") for _ in range(count)]


@override_settings(CACHES=_LOCMEM)
class DownSourceTests(_OllamaCase):
    """A down source is asked twice a run at most, then less and less often; the outage costs no photo an attempt."""

    def test_two_failures_in_a_row_stop_the_source_for_the_run(self) -> None:
        for _ in range(4):
            _due(_photo())
        request = self.http(*_down(4))

        outcomes = keyword_retry.sweep()

        self.assertEqual(request.call_count, keyword_retry.FAILURES_THAT_STOP_A_SOURCE)
        self.assertEqual(outcomes[KeywordOutcome.UNANSWERED], keyword_retry.FAILURES_THAT_STOP_A_SOURCE)

    def test_a_source_that_failed_is_not_asked_again_until_its_backoff_ends(self) -> None:
        for _ in range(3):
            _due(_photo())
        request = self.http(*_down(3))

        keyword_retry.sweep()
        keyword_retry.sweep()
        self.assertEqual(request.call_count, 2, "the second run asked a source still backing off")

        with mock.patch(_TIME, return_value=keyword_retry.time.time() + 16 * 60):
            keyword_retry.sweep()
        self.assertEqual(request.call_count, 3)

    def test_each_failing_run_backs_the_source_off_longer(self) -> None:
        from urbanlens.dashboard.services.core.provider_health import backoff_duration

        for _ in range(6):
            _due(_photo())
        self.http(*_down(6))
        clock = keyword_retry.time.time()
        waits = []
        for _ in range(3):
            with mock.patch(_TIME, return_value=clock):
                keyword_retry.sweep()
            level, until = keyword_retry.source_backoff(_OLLAMA)
            waits.append(round(until - clock))
            clock = until + 1

        self.assertEqual(waits, [round(backoff_duration(level).total_seconds()) for level in (1, 2, 3)])

    def test_an_answer_ends_the_backoff(self) -> None:
        first, second = _photo(), _photo()
        _due(first, ago=timedelta(minutes=2))
        self.http(*_down(1), _answer())
        keyword_retry.sweep()
        _due(second)

        with mock.patch(_TIME, return_value=keyword_retry.time.time() + 16 * 60):
            keyword_retry.sweep()

        self.assertEqual(keyword_retry.source_backoff(_OLLAMA)[0], 0)

    def test_an_answer_to_an_upload_ends_the_backoff(self) -> None:
        _due(_photo())
        self.http(*_down(1), _answer())
        keyword_retry.sweep()
        self.assertEqual(keyword_retry.source_backoff(_OLLAMA)[0], 1)

        generate_keywords_for_image(_photo().pk)

        self.assertEqual(keyword_retry.source_backoff(_OLLAMA)[0], 0)

    def test_an_outage_spends_none_of_a_photos_attempts(self) -> None:
        image = _photo()
        _due(image)
        self.http(*_down(4))
        clock = keyword_retry.time.time()
        for _ in range(4):
            ImageKeywordRetry.objects.filter(image=image).update(retry_at=timezone.now() - timedelta(minutes=1))
            with mock.patch(_TIME, return_value=clock):
                keyword_retry.sweep()
            clock = keyword_retry.source_backoff(_OLLAMA)[1] + 1

        self.assertEqual(_row(image).attempts, 0)
        self.assertEqual(keyword_retry.source_backoff(_OLLAMA)[0], 4)

    def test_a_failure_before_the_source_answers_another_counts_against_the_photo(self) -> None:
        poison, good = _photo(), _photo()
        _due(poison, ago=timedelta(minutes=2))
        _due(good, ago=timedelta(minutes=1))
        self.http(requests.ConnectionError("this photo, every time"), _answer())

        keyword_retry.sweep()

        self.assertIsNone(_row(good))
        row = _row(poison)
        self.assertEqual(row.attempts, 1)
        self.assertGreater(row.retry_at, timezone.now() + keyword_retry.RETRY_DELAYS[0])

    def test_a_failure_after_the_source_answered_another_counts_against_the_photo(self) -> None:
        good, poison = _photo(), _photo()
        _due(good, ago=timedelta(minutes=2))
        _due(poison, ago=timedelta(minutes=1))
        self.http(_answer(), requests.ConnectionError("this photo, every time"))

        keyword_retry.sweep()

        self.assertIsNone(_row(good))
        self.assertEqual(_row(poison).attempts, 1)

    def test_a_photo_the_source_cannot_answer_does_not_hold_the_source_back(self) -> None:
        poison, good = _photo(), _photo()
        _due(poison, ago=timedelta(minutes=2))
        _due(good, ago=timedelta(minutes=1))
        self.http(requests.ConnectionError("this photo, every time"), _answer())

        keyword_retry.sweep()

        self.assertEqual(keyword_retry.source_backoff(_OLLAMA), (0, 0.0))

    def test_a_photo_failing_while_its_source_answers_others_is_given_up(self) -> None:
        poison, good = _photo(), _photo()
        ImageKeywordRetry.objects.create(
            image=poison,
            source=_OLLAMA,
            attempts=keyword_retry.MAX_ATTEMPTS - 1,
            retry_at=timezone.now() - timedelta(minutes=2),
        )
        _due(good, ago=timedelta(minutes=1))
        self.http(requests.ConnectionError("this photo, every time"), _answer())

        keyword_retry.sweep()

        row = _row(poison)
        self.assertEqual(row.attempts, keyword_retry.MAX_ATTEMPTS)
        self.assertIsNone(row.retry_at)

    def test_a_photo_failing_alone_is_given_up_once_it_has_failed_for_too_long(self) -> None:
        # Nothing else waits on the source and no upload answers, so its failures look like an outage and never count.
        poison = _photo()
        row = _due(poison)
        ImageKeywordRetry.objects.filter(pk=row.pk).update(
            first_failed_at=timezone.now() - keyword_retry.MAX_WAIT + timedelta(hours=1)
        )
        request = self.http(*_down(2))

        keyword_retry.sweep()
        self.assertIsNotNone(_row(poison).retry_at, "given up before MAX_WAIT")

        ImageKeywordRetry.objects.filter(pk=row.pk).update(
            first_failed_at=timezone.now() - keyword_retry.MAX_WAIT - timedelta(hours=1),
            retry_at=timezone.now() - timedelta(minutes=1),
        )
        with mock.patch(_TIME, return_value=keyword_retry.source_backoff(_OLLAMA)[1] + 1):
            keyword_retry.sweep()

        self.assertEqual(request.call_count, 2)
        self.assertEqual(_row(poison).attempts, 0)
        self.assertIsNone(_row(poison).retry_at)

    def test_the_wait_before_giving_up_runs_from_the_first_failure_not_from_a_refusal(self) -> None:
        # A row an upload's refusal wrote long ago, its source closed since, then failing once: not 30 days of failing.
        image = _photo()
        row = _due(image)
        ImageKeywordRetry.objects.filter(pk=row.pk).update(
            created=timezone.now() - keyword_retry.MAX_WAIT - timedelta(days=1)
        )
        self.http(*_down(1))

        keyword_retry.sweep()

        retry = _row(image)
        self.assertIsNotNone(retry.retry_at)
        self.assertGreater(retry.first_failed_at, timezone.now() - timedelta(minutes=1))

    def test_a_soft_time_limit_stops_the_run_and_still_records_what_it_saw(self) -> None:
        from celery.exceptions import SoftTimeLimitExceeded

        failed, cut_short = _photo(), _photo()
        _due(failed, ago=timedelta(minutes=2))
        row = _due(cut_short, ago=timedelta(minutes=1))
        self.http(requests.ConnectionError("connection refused"), SoftTimeLimitExceeded())

        with self.assertRaises(SoftTimeLimitExceeded):
            keyword_retry.sweep()

        self.assertGreater(_row(failed).retry_at, timezone.now())
        self.assertEqual(_row(failed).attempts, 0)
        self.assertEqual((_row(cut_short).attempts, _row(cut_short).retry_at), (0, row.retry_at))

    def test_a_database_error_after_an_answer_does_not_back_the_source_off(self) -> None:
        from django.db import OperationalError

        poison, good = _photo(), _photo()
        _due(poison, ago=timedelta(minutes=2))
        _due(good, ago=timedelta(minutes=1))
        self.http(requests.ConnectionError("this photo, every time"), _answer())
        real_forget = keyword_retry.forget

        def forget(image_id: int, source: str) -> None:
            if image_id == good.pk:
                raise OperationalError("the database went away")
            real_forget(image_id, source)

        with mock.patch.object(keyword_retry, "forget", side_effect=forget):
            keyword_retry.sweep()

        self.assertEqual(keyword_retry.source_backoff(_OLLAMA), (0, 0.0))
        self.assertEqual(_row(poison).attempts, 1)
        self.assertEqual(_stored(good), {"mill", "smokestack"})


@override_settings(CACHES=_LOCMEM)
class RefusedSourceTests(_OllamaCase):
    """A source refused before anything is sent is skipped, and the photos waiting on it are not held to blame."""

    def test_a_backed_off_provider_is_not_asked(self) -> None:
        from urbanlens.dashboard.models.provider_health import ProviderHealth
        from urbanlens.dashboard.models.provider_health.meta import ProviderState
        from urbanlens.dashboard.services.core import provider_health

        image = _photo()
        row = _due(image)
        ProviderHealth.objects.create(
            provider="ollama", state=ProviderState.BACKED_OFF, backed_off_until=timezone.now() + timedelta(hours=1)
        )
        provider_health.write_snapshot()
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()
        self.assertEqual(_row(image).retry_at, row.retry_at)
        self.assertEqual(_row(image).attempts, 0)

    def test_a_switched_off_service_is_not_asked(self) -> None:
        from urbanlens.dashboard.models.api_rate_limit.model import ApiRateLimit
        from urbanlens.dashboard.services.core.rate_limiter import get_limit_config

        image = _photo()
        _due(image)
        get_limit_config("ollama")
        ApiRateLimit.objects.filter(service="ollama").update(enabled=False)
        cache.clear()
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()
        self.assertIsNotNone(_row(image))

    def test_a_refusal_for_now_stops_the_source_and_holds_nothing_against_the_photo(self) -> None:
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        photos = [_photo() for _ in range(3)]
        rows = [_due(image) for image in photos]
        request = self.http()
        with mock.patch(
            "urbanlens.dashboard.services.core.rate_limiter._reserve_call", side_effect=RateLimitExceededError("ollama")
        ) as reserve:
            outcomes = keyword_retry.sweep()

        request.assert_not_called()
        self.assertEqual(reserve.call_count, 1)
        self.assertEqual(outcomes[KeywordOutcome.BUSY], 1)
        for image, row in zip(photos, rows, strict=True):
            self.assertEqual((_row(image).attempts, _row(image).retry_at), (0, row.retry_at))
        self.assertEqual(
            keyword_retry.source_backoff(_OLLAMA)[0], 0, "a refusal is the rate limiter's business, not a failure"
        )

    def test_a_photo_without_an_analysis_copy_is_dropped_for_the_backfill_without_a_call(self) -> None:
        missing, fine = _photo(analysis_copy=False), _photo()
        _due(missing, ago=timedelta(minutes=2))
        _due(fine, ago=timedelta(minutes=1))
        request = self.http(_answer())

        outcomes = keyword_retry.sweep()

        self.assertEqual(request.call_count, 1)
        self.assertIsNone(_row(missing))
        self.assertIsNone(_row(fine))
        self.assertEqual(outcomes[keyword_retry.DROPPED], 1)

    def test_photos_without_an_analysis_copy_do_not_hold_back_a_healthy_one(self) -> None:
        for minutes in range(25):
            _due(_photo(analysis_copy=False), ago=timedelta(minutes=30 - minutes))
        fine = _photo()
        _due(fine)
        request = self.http(_answer())

        keyword_retry.sweep()

        self.assertEqual(request.call_count, 1)
        self.assertIsNone(_row(fine))

    def _unreadable(self, *unreadable: Image) -> mock.MagicMock:
        """Make these photos' analysis copies unreadable, as a storage failure would."""
        from urbanlens.dashboard.services.photos import photo_keywords

        real = photo_keywords.require_analysis_jpeg_bytes
        broken = {image.pk for image in unreadable}

        def read(image: Image) -> bytes:
            if image.pk in broken:
                raise photo_keywords.AnalysisCopyUnavailableError(f"image {image.pk} has no readable analysis copy")
            return real(image)

        return self.enterContext(
            mock.patch("urbanlens.dashboard.plugins.builtin.ollama.require_analysis_jpeg_bytes", side_effect=read)
        )

    def test_an_unreadable_copy_counts_when_the_run_read_another(self) -> None:
        broken, fine = _photo(), _photo()
        _due(broken, ago=timedelta(minutes=2))
        _due(fine, ago=timedelta(minutes=1))
        self._unreadable(broken)
        request = self.http(_answer())

        keyword_retry.sweep()

        self.assertEqual(request.call_count, 1)
        self.assertEqual(_row(broken).attempts, 1)
        self.assertIsNone(_row(fine))

    def test_unreadable_copies_with_nothing_read_cost_no_attempt(self) -> None:
        # Storage is down: every copy fails to read, which is no photo's fault.
        photos = [_photo() for _ in range(3)]
        for image in photos:
            _due(image)
        self._unreadable(*photos)
        request = self.http()

        keyword_retry.sweep()

        request.assert_not_called()
        self.assertEqual([_row(image).attempts for image in photos], [0, 0, 0])
        self.assertTrue(all(_row(image).retry_at > timezone.now() for image in photos))


@override_settings(CACHES=_LOCMEM)
class HostedSourceEgressTests(TestCase):
    """Development makes no hosted AI call, from the sweep either, and the photos waiting on one are left as they were."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_development_does_not_ask_a_hosted_source(self) -> None:
        from urbanlens.dashboard.plugins.builtin.photo_keywords import (
            AiVisionKeywordProvider,
            ClassifierKeywordProvider,
        )
        from urbanlens.dashboard.tests.hypothesis.test_ai_refused_in_development import DEVELOPMENT, deployment

        image = _photo()
        rows = [_due(image, source) for source in (AiVisionKeywordProvider.slug, ClassifierKeywordProvider.slug)]
        client = mock.Mock()
        with (
            deployment(DEVELOPMENT),
            mock.patch(_REGISTRY, return_value=[AiVisionKeywordProvider(), ClassifierKeywordProvider()]),
            mock.patch("urbanlens.dashboard.services.ai.inference_client.get_inference_client", return_value=client),
        ):
            keyword_retry.sweep()

        client.send.assert_not_called()
        client.classify.assert_not_called()
        for row in rows:
            self.assertEqual(ImageKeywordRetry.objects.get(pk=row.pk).retry_at, row.retry_at)

    def test_every_built_in_provider_that_calls_out_names_its_service(self) -> None:
        from urbanlens.dashboard.plugins.builtin.ollama import OllamaVisionKeywordProvider
        from urbanlens.dashboard.plugins.builtin.photo_keywords import (
            AiVisionKeywordProvider,
            ClassifierKeywordProvider,
            MetadataKeywordProvider,
        )
        from urbanlens.dashboard.services.ai.vision import SERVICE_AI_PHOTO_KEYWORDS, SERVICE_PHOTO_CLASSIFIER
        from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway

        self.assertEqual(AiVisionKeywordProvider.service_key, SERVICE_AI_PHOTO_KEYWORDS)
        self.assertEqual(ClassifierKeywordProvider.service_key, SERVICE_PHOTO_CLASSIFIER)
        self.assertEqual(OllamaVisionKeywordProvider.service_key, OllamaGateway.service_key)
        self.assertEqual(MetadataKeywordProvider.service_key, "")


class SweepTaskTests(SimpleTestCase):
    """The beat entry is scheduled, classified as reaching outside, gated in the task, and locked for less than its interval."""

    def test_the_entry_is_scheduled_and_external(self) -> None:
        from django.conf import settings

        from urbanlens.UrbanLens.egress import BEAT_EGRESS, BeatEgress

        entry = settings.CELERY_BEAT_SCHEDULE["keyword-retry-sweep"]
        self.assertEqual(entry["task"], "urbanlens.dashboard.tasks.sweep_keyword_retries")
        self.assertIs(BEAT_EGRESS["keyword-retry-sweep"], BeatEgress.EXTERNAL)

    def test_the_task_is_gated(self) -> None:
        from urbanlens.dashboard.services.core.egress import GATED_BACKGROUND_TASKS

        self.assertIn("urbanlens.dashboard.tasks.sweep_keyword_retries", GATED_BACKGROUND_TASKS["keyword-retry-sweep"])

    def test_the_task_runs_where_nobody_waits(self) -> None:
        from urbanlens.dashboard.services.core.background_work import queue_is_background
        from urbanlens.dashboard.tasks import sweep_keyword_retries

        self.assertTrue(queue_is_background(sweep_keyword_retries.queue))


@override_settings(CACHES=_LOCMEM)
class SweepTaskRunTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_development_skips_the_whole_sweep(self) -> None:
        from urbanlens.dashboard.tasks import sweep_keyword_retries
        from urbanlens.dashboard.tests.hypothesis.test_ai_refused_in_development import DEVELOPMENT, deployment

        with (
            deployment(DEVELOPMENT),
            mock.patch("urbanlens.dashboard.services.photos.keyword_retry.sweep") as sweep,
        ):
            self.assertIsNone(sweep_keyword_retries())
        sweep.assert_not_called()

    def test_an_overlapping_run_does_nothing(self) -> None:
        from urbanlens.dashboard.services.core.locks import beat_lock
        from urbanlens.dashboard.tasks import (
            _KEYWORD_RETRY_SWEEP_LOCK_KEY,
            _KEYWORD_RETRY_SWEEP_LOCK_SECONDS,
            sweep_keyword_retries,
        )

        with (
            beat_lock(_KEYWORD_RETRY_SWEEP_LOCK_KEY, _KEYWORD_RETRY_SWEEP_LOCK_SECONDS),
            mock.patch("urbanlens.dashboard.services.photos.keyword_retry.sweep") as sweep,
        ):
            self.assertEqual(sweep_keyword_retries(), {})
        sweep.assert_not_called()


class RaiseRefusalTests(TestCase):
    """The keyword calls let a refusal through when asked to, so a caller can tell "not asked" from "no answer"."""

    def test_the_vision_calls_raise_a_refusal_only_when_asked(self) -> None:
        from urbanlens.dashboard.services.ai import vision
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        refusal = RateLimitExceededError(vision.SERVICE_PHOTO_CLASSIFIER)
        with mock.patch.object(vision, "api_call_slot", side_effect=refusal):
            self.assertIsNone(vision.classify_photo(_jpeg()))
            self.assertIsNone(vision.describe_photo_keywords(_jpeg()))
            with self.assertRaises(RateLimitExceededError):
                vision.classify_photo(_jpeg(), raise_refusal=True)
            with self.assertRaises(RateLimitExceededError):
                vision.describe_photo_keywords(_jpeg(), raise_refusal=True)

    def test_the_ollama_call_raises_a_refusal_only_when_asked(self) -> None:
        from urbanlens.dashboard.services.apis.ai.ollama import OllamaGateway
        from urbanlens.dashboard.services.core.rate_limiter import RateLimitExceededError

        gateway = OllamaGateway(base_url=_OLLAMA_URL, model="llava")
        with mock.patch(
            "urbanlens.dashboard.services.core.rate_limiter._reserve_call", side_effect=RateLimitExceededError("ollama")
        ):
            self.assertIsNone(gateway.describe_photo_keywords(_jpeg()))
            with self.assertRaises(RateLimitExceededError):
                gateway.describe_photo_keywords(_jpeg(), raise_refusal=True)
