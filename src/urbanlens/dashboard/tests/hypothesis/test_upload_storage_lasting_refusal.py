"""Storage refusing for a reason that needs an operator is not "briefly unavailable", and the admins hear of it (P241).

A rotated key the app never picked up, or a bucket policy that denies writes, answers every request with a 4xx. That is
a refusal no amount of waiting fixes, so a client is not told to retry in thirty seconds, and the admins are told
straight away rather than only once storage serves something else.
"""

from __future__ import annotations

import json
from unittest import mock

from botocore.awsrequest import AWSRequest, AWSResponse
from botocore.exceptions import ClientError
from django.core.cache import cache
from django.http import HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.services.media import upload_retry
from urbanlens.dashboard.services.media.storage import (
    StorageUnavailableError,
    storage_failures_refused,
    storage_refusal,
)
from urbanlens.dashboard.tests.hypothesis.test_object_store_client_config import _Body, object_store, writes_fail
from urbanlens.dashboard.tests.hypothesis.test_upload_storage_outage import _CORNERS, _jpeg, _OutageCase

_NOTIFY = "urbanlens.dashboard.services.notifications.notifications.notify"
_ACCESS_DENIED = b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>InvalidAccessKeyId</Code><Message>The access key does not exist</Message></Error>'


def garage_rejects_the_key(request: AWSRequest) -> AWSResponse:
    """What Garage answers a request signed with a key it doesn't know."""
    body = b"" if request.method == "HEAD" else _ACCESS_DENIED
    return AWSResponse(
        request.url, 403, {"Content-Type": "application/xml", "Content-Length": str(len(body))}, _Body(body)
    )


def _client_error(code: str, status: int) -> ClientError:
    return ClientError({"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")


class _NotifyPatchedCase(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.delete(upload_retry.REFUSAL_REPORTED_KEY)
        self.addCleanup(cache.delete, upload_retry.REFUSAL_REPORTED_KEY)
        patcher = mock.patch(_NOTIFY)
        self.notify = patcher.start()
        self.addCleanup(patcher.stop)


class RefusalWordingTests(_NotifyPatchedCase):
    def test_a_rejected_key_is_not_called_brief_and_sets_no_retry_after(self) -> None:
        refusal = storage_refusal(_client_error("InvalidAccessKeyId", 403), "test")

        assert refusal is not None
        self.assertIsNone(refusal.retry_after)
        self.assertNotIn("briefly", str(refusal))
        self.assertNotIn("Retry-After", refusal.headers)

    def test_a_quorum_503_is_still_brief_and_retryable(self) -> None:
        refusal = storage_refusal(_client_error("ServiceUnavailable", 503), "test")

        assert refusal is not None
        self.assertEqual(refusal.retry_after, 30)
        self.assertIn("briefly", str(refusal))

    def test_the_block_helper_words_it_the_same_way(self) -> None:
        with self.assertRaises(StorageUnavailableError) as raised, storage_failures_refused():
            raise _client_error("AccessDenied", 403)

        self.assertIsNone(raised.exception.retry_after)

    def test_the_middleware_sends_no_retry_after_for_it(self) -> None:
        from urbanlens.dashboard.middleware import StorageUnavailableMiddleware

        middleware = StorageUnavailableMiddleware(lambda request: HttpResponse())
        response = middleware.process_exception(
            RequestFactory().post("/anything/"), _client_error("SignatureDoesNotMatch", 403)
        )

        assert response is not None
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Retry-After", response)


class AdminsAreToldTests(_NotifyPatchedCase):
    def test_a_rejected_key_tells_the_admins_once_per_interval(self) -> None:
        for _ in range(3):
            storage_refusal(_client_error("InvalidAccessKeyId", 403), "test")

        self.notify.assert_called_once()
        self.assertIn("InvalidAccessKeyId", self.notify.call_args.args[2])

    def test_a_transient_failure_tells_no_one(self) -> None:
        storage_refusal(_client_error("ServiceUnavailable", 503), "test")

        self.notify.assert_not_called()

    def test_a_missing_file_tells_no_one(self) -> None:
        upload_retry.report_lasting_refusal(_client_error("NoSuchKey", 404))
        upload_retry.report_lasting_refusal(FileNotFoundError("gone"))

        self.notify.assert_not_called()

    def test_processing_that_waits_on_a_rejected_key_tells_them(self) -> None:
        with mock.patch.object(upload_retry, "_keep_waiting"):
            upload_retry.wait_for_storage(
                upload_retry.IMAGE, 1, "photos/x.jpg", _client_error("InvalidAccessKeyId", 403)
            )

        self.notify.assert_called_once()

    def test_an_unreachable_cache_does_not_turn_into_a_notification_storm(self) -> None:
        from redis.exceptions import ConnectionError as RedisConnectionError

        with mock.patch.object(upload_retry.cache, "add", side_effect=RedisConnectionError("down")):
            upload_retry.report_lasting_refusal(_client_error("InvalidAccessKeyId", 403))

        self.notify.assert_not_called()


class UploadThroughARejectedKeyTests(_OutageCase):
    def test_the_overlay_uploader_keeps_the_lasting_wording(self) -> None:
        with mock.patch(_NOTIFY), object_store(writes_fail(garage_rejects_the_key)):
            response = self.client.post(
                reverse("pin.overlays", args=[self.pin.slug]),
                {"corners": json.dumps(_CORNERS), "name": "Sheet", "image": _jpeg()},
            )

        self.assertEqual(response.status_code, 503, response.content[:300])
        self.assertNotIn("Retry-After", response)
        self.assertNotIn(b"briefly", response.content)

    def test_the_gallery_upload_is_a_503_without_retry_after_and_nothing_stored(self) -> None:
        with mock.patch(_NOTIFY), object_store(writes_fail(garage_rejects_the_key)):
            response = self.client.post(reverse("pin.gallery", args=[self.pin.slug]), {"image": _jpeg()})

        self.assertEqual(response.status_code, 503, response.content[:300])
        self.assertNotIn("Retry-After", response)
        self.assertNotIn(b"briefly", response.content)
        self.assertFalse(Image.objects.filter(profile=self.profile).exists())
