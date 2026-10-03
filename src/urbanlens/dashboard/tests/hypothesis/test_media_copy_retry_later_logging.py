"""media-copy's "not yet" 503 is an answer, not an error; a real failure on the same view still is one (P204)."""

from __future__ import annotations

from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory
from django.utils.log import log_response

from urbanlens.core.tests.log_output import handler_output
from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.remote_image_copy.model import RemoteImageCopy
from urbanlens.dashboard.services.media.previews import RENDER_QUEUED
from urbanlens.dashboard.services.media.remote_copies import copy_url, pending_marker

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_VIEW = "urbanlens.dashboard.controllers.remote_copies"


class MediaCopyRetryLaterLoggingTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.url = copy_url("https://provider.test/p204.jpg", provider="test")
        self.copy = RemoteImageCopy.objects.get()
        self.addCleanup(cache.delete, pending_marker(self.copy.url_digest))

    def _get(self, client: Client | None = None) -> tuple[int, bool, str]:
        with handler_output("django.request") as output:
            response = (client or self.client).get(self.url)
        return response.status_code, response.has_header("Retry-After"), output.getvalue()

    def test_the_request_that_starts_the_copy_is_not_an_error(self) -> None:
        with patch(_ENQUEUE):
            status, retry_after, logged = self._get()

        self.assertEqual((status, retry_after), (503, True))
        self.assertNotIn(self.url, logged)

    def test_a_request_while_the_copy_is_being_made_is_not_an_error(self) -> None:
        cache.set(pending_marker(self.copy.url_digest), RENDER_QUEUED, 60)

        status, retry_after, logged = self._get()

        self.assertEqual((status, retry_after), (503, True))
        self.assertNotIn(self.url, logged)

    def test_a_caller_over_the_copy_rate_is_not_an_error(self) -> None:
        with patch("urbanlens.dashboard.services.security.throttle.allow", return_value=False), patch(_ENQUEUE):
            status, retry_after, logged = self._get()

        self.assertEqual((status, retry_after), (503, True))
        self.assertNotIn(self.url, logged)

    def test_a_failure_before_the_answer_is_still_an_error(self) -> None:
        with patch(f"{_VIEW}.pending_marker", side_effect=RuntimeError("cache unreachable")):
            status, _, logged = self._get(Client(raise_request_exception=False))

        self.assertEqual(status, 500)
        self.assertIn("ERROR django.request", logged)
        self.assertIn(self.url, logged)
        self.assertIn("cache unreachable", logged)

    def test_a_failure_after_the_view_chose_to_retry_later_is_still_an_error(self) -> None:
        """The request is marked by then; only the 503 it was marked for is dropped."""
        cache.set(pending_marker(self.copy.url_digest), RENDER_QUEUED, 60)
        with patch(f"{_VIEW}.HttpResponse", side_effect=RuntimeError("response could not be built")):
            status, _, logged = self._get(Client(raise_request_exception=False))

        self.assertEqual(status, 500)
        self.assertIn("ERROR django.request", logged)
        self.assertIn(self.url, logged)


class UnmarkedServiceUnavailableTests(SimpleTestCase):
    def test_a_503_no_view_chose_is_still_an_error(self) -> None:
        request = RequestFactory().get("/dashboard/map/media-copy/abc/")

        with handler_output("django.request") as output:
            log_response(
                "%s: %s", "Service Unavailable", request.path, response=HttpResponse(status=503), request=request
            )

        self.assertIn("ERROR django.request", output.getvalue())
        self.assertIn("Service Unavailable: /dashboard/map/media-copy/abc/", output.getvalue())
