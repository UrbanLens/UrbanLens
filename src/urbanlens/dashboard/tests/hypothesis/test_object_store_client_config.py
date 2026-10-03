"""The media object store's client gives up inside the proxy's timeout, after a bounded number of attempts (P201).

botocore's defaults are five attempts with a 60 s read timeout, so a stalled Garage write could hold a request for
about five minutes, long after Cloudflare had answered the browser with an error at 100 s.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import uuid

from botocore.awsrequest import AWSRequest, AWSResponse
from botocore.exceptions import ReadTimeoutError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage, storages
from django.test import override_settings

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.services.media.object_storage import GatedS3Storage
from urbanlens.dashboard.services.media.storage import UPLOAD_RESERVATION_WAIT_SECONDS
from urbanlens.dashboard.services.media.storage_errors import STORAGE_ERRORS
from urbanlens.UrbanLens.settings.app import settings as app_settings
from urbanlens.UrbanLens.settings.base import _S3_STORAGE_OPTIONS

#: Cloudflare answers the browser itself once an origin has been silent this long.
PROXY_TIMEOUT_SECONDS = 100

#: The test options: production's, pointed at an endpoint nothing listens on, with credentials that sign.
TEST_S3_OPTIONS = {
    **_S3_STORAGE_OPTIONS,
    "bucket_name": "ul-media",
    "access_key": "test",
    "secret_key": "test",
    "region_name": "garage",
    "endpoint_url": "http://objectstore:3900",
}

_GARAGE_503 = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b"<Error><Code>ServiceUnavailable</Code><Message>Could not reach quorum of 2. 1 of 2 request succeeded, "
    b"others returned errors: Not connected</Message></Error>"
)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def stream(self, **_: object) -> Iterator[bytes]:
        yield self._data


def garage_unavailable(request: AWSRequest) -> AWSResponse:
    """What Garage answers while a node is down: a quorum 503 (a HEAD gets the status with no body)."""
    body = b"" if request.method == "HEAD" else _GARAGE_503
    return AWSResponse(
        request.url, 503, {"Content-Type": "application/xml", "Content-Length": str(len(body))}, _Body(body)
    )


def garage_stalled(request: AWSRequest) -> AWSResponse:
    """What botocore raises when Garage holds a request open past the read timeout."""
    raise ReadTimeoutError(endpoint_url=request.url)


def writes_fail(fail: Callable[[AWSRequest], AWSResponse]) -> Callable[[AWSRequest], AWSResponse]:
    """Answer a name check with "no such key", and every write with *fail*.

    A name that is free is what storage checks before writing, since this deployment never overwrites.
    """

    def respond(request: AWSRequest) -> AWSResponse:
        if request.method == "HEAD":
            return AWSResponse(request.url, 404, {"Content-Length": "0"}, _Body(b""))
        return fail(request)

    return respond


@contextmanager
def object_store(respond: Callable[[AWSRequest], AWSResponse]) -> Iterator[list[AWSRequest]]:
    """Put default storage on the real S3 backend, answering every request it sends with *respond*.

    Every file field without a storage of its own follows ``default_storage``, so this is the backend each upload path
    writes through in production, built from production's options.

    Yields:
        Each request sent, in order, retries included.
    """
    sent: list[AWSRequest] = []

    def handler(request: AWSRequest, **_: object) -> AWSResponse:
        sent.append(request)
        return respond(request)

    backends = {
        "default": {
            "BACKEND": "urbanlens.dashboard.services.media.object_storage.GatedS3Storage",
            "OPTIONS": TEST_S3_OPTIONS,
        },
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    with override_settings(STORAGES=backends, UL_MEDIA_STORAGE_BACKEND="s3"):
        storage = storages["default"]
        assert isinstance(storage, GatedS3Storage)
        assert default_storage.connection is storage.connection
        storage.connection.meta.client.meta.events.register("before-send.s3", handler)
        yield sent


class ObjectStoreClientConfigTests(SimpleTestCase):
    def _client_config(self):
        return GatedS3Storage(**TEST_S3_OPTIONS).connection.meta.client.meta.config

    def test_the_client_retries_in_standard_mode_with_bounded_attempts(self) -> None:
        config = self._client_config()
        self.assertEqual(config.retries.get("mode"), "standard")
        self.assertEqual(config.retries.get("total_max_attempts"), app_settings.s3_max_attempts)

    def test_the_client_times_out_as_configured(self) -> None:
        config = self._client_config()
        self.assertEqual(config.connect_timeout, app_settings.s3_connect_timeout_seconds)
        self.assertEqual(config.read_timeout, app_settings.s3_read_timeout_seconds)

    def test_an_upload_request_gives_up_on_a_stalled_store_inside_the_proxy_timeout(self) -> None:
        """An upload makes two calls (the name check, then the write); a call fails only after every attempt has.

        The worst case is the name check stalling on every attempt and answering just inside its last, then the write
        stalling on all of them, after waiting the whole reservation for the uploader's previous upload.
        """
        config = self._client_config()
        attempts = config.retries["total_max_attempts"]
        per_attempt = config.connect_timeout + config.read_timeout
        # botocore's standard backoff for a non-throttling error is at most a second per retry.
        backoff = 2 * (attempts - 1)
        worst = UPLOAD_RESERVATION_WAIT_SECONDS + 2 * attempts * per_attempt + backoff
        self.assertLess(worst, PROXY_TIMEOUT_SECONDS - 10)

    def test_the_client_still_addresses_garage_by_path_and_signs_with_sigv4(self) -> None:
        """S3Storage drops its addressing_style and signature_version options once a client_config is passed."""
        with object_store(garage_unavailable) as sent, self.assertRaises(STORAGE_ERRORS):
            default_storage.exists("pin_images/x.jpg")
        self.assertTrue(sent[0].url.startswith("http://objectstore:3900/ul-media/pin_images/x.jpg"), sent[0].url)
        self.assertTrue(sent[0].headers["Authorization"].startswith(b"AWS4-HMAC-SHA256"))

    def test_a_name_check_garage_refuses_is_tried_the_configured_number_of_times(self) -> None:
        for respond in (garage_unavailable, garage_stalled):
            with self.subTest(respond=respond.__name__), object_store(respond) as sent:
                with self.assertRaises(STORAGE_ERRORS):
                    default_storage.save(f"pin_images/{uuid.uuid4().hex}.jpg", ContentFile(b"bytes"))
                self.assertEqual([request.method for request in sent], ["HEAD"] * app_settings.s3_max_attempts)

    def test_a_write_garage_refuses_is_tried_the_configured_number_of_times(self) -> None:
        for fail in (garage_unavailable, garage_stalled):
            with self.subTest(fail=fail.__name__), object_store(writes_fail(fail)) as sent:
                with self.assertRaises(STORAGE_ERRORS):
                    default_storage.save(f"pin_images/{uuid.uuid4().hex}.jpg", ContentFile(b"bytes"))
                self.assertEqual(
                    [request.method for request in sent], ["HEAD", *["PUT"] * app_settings.s3_max_attempts]
                )
