"""Four unauthenticated proxies wrote third-party bytes into the shared Valkey.

`RedataMediaProxyMixin.serve_media` downloads a file from REData and does
`cache.set(cache_key, original, 3600)` with no size bound, into the same 512MB
instance that holds sessions, the Channels layer and the Celery broker under
`volatile-lru` - so a large enough body does not merely waste space, it evicts
other people's sessions. None of the four views requires a login, and the cache
key is built from path parameters the caller chooses, so the number of distinct
entries is the caller's to decide as well (N21 H14).

Two bounds, because one without the other does nothing:

* a per-entry ceiling, so a single body cannot be arbitrarily large - documents
  here, not thumbnails, so the ceiling is larger than `bounded_cache`'s default
  and named at the call site rather than inherited by accident;
* a rate, so the *number* of entries one caller can mint is bounded too. These
  are GETs, and `throttled` counted only unsafe methods - which was right for the
  signup and demo POSTs it was written for and wrong here, where GET is the
  expensive method.

Refusing to cache must never mean refusing to answer: the body is served either
way, which is `bounded_cache.set_if_small`'s contract and is asserted below.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.urls import resolve, reverse

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.services.security import throttle


class TheProxyCachesOnlyWhatItShouldTests(TestCase):
    """The per-entry half."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.url = reverse("pin.loopnet.photo", kwargs={"listing_uuid": "abc", "photo_id": 1})

    def _serve(self, body: bytes) -> object:
        """Fetch the proxy with a stubbed REData download returning *body*."""
        with mock.patch(
            "urbanlens.dashboard.services.apis.property_records.redata_gateway.RedataGateway.download_listing_photo",
            return_value=(body, "image/jpeg"),
        ):
            return self.client.get(self.url)

    def test_an_ordinary_photo_is_served_and_cached(self) -> None:
        """The positive half. Without it, the refusal test below would pass just
        as well against a proxy that had stopped caching altogether."""
        with mock.patch("urbanlens.dashboard.services.core.bounded_cache.cache.set") as stored:
            response = self._serve(b"x" * 1024)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(bytes(response.content), b"x" * 1024)
        stored.assert_called_once()
        self.assertEqual(stored.call_args.args[1], (b"x" * 1024, "image/jpeg"))

    def test_an_oversized_body_is_still_served(self) -> None:
        """The failure that would be worse than the bug: bounding the cache by
        refusing the request."""
        from urbanlens.dashboard.controllers.pin import REDATA_MEDIA_MAX_CACHED_BYTES

        body = b"x" * (REDATA_MEDIA_MAX_CACHED_BYTES + 1)

        response = self._serve(body)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), len(body))

    def test_an_oversized_body_is_not_cached(self) -> None:
        from urbanlens.dashboard.controllers.pin import REDATA_MEDIA_MAX_CACHED_BYTES

        with mock.patch("urbanlens.dashboard.services.core.bounded_cache.cache.set") as stored:
            self._serve(b"x" * (REDATA_MEDIA_MAX_CACHED_BYTES + 1))

        stored.assert_not_called()

    def test_the_ceiling_is_larger_than_the_thumbnail_default(self) -> None:
        """These are scanned PDFs and TIFFs. Inheriting the thumbnail ceiling
        would refuse to cache almost all of them, turning every view into a
        fresh REData download - a different resource spent, not a saving."""
        from urbanlens.dashboard.controllers.pin import REDATA_MEDIA_MAX_CACHED_BYTES
        from urbanlens.dashboard.services.core.bounded_cache import MAX_CACHED_BODY_BYTES

        self.assertGreater(REDATA_MEDIA_MAX_CACHED_BYTES, MAX_CACHED_BODY_BYTES)


class TheProxyIsRateLimitedTests(TestCase):
    """The how-many half. Without it the per-entry ceiling bounds one entry.

    Asserted off the URLconf rather than by making six hundred requests: a
    behavioural test at this limit is slow, and - worse - a route somebody
    forgot to wrap would make it pass by never reaching the limit at all.
    """

    #: Every REData proxy route, by name. Listed rather than discovered, so
    #: adding a fifth one without a limiter fails here.
    ROUTES = (
        ("pin.loopnet.photo", {"listing_uuid": "abc", "photo_id": 1}),
        ("pin.cris.attachment", {"resource_uuid": "abc", "attachment_id": 1}),
        ("pin.cris.extracted_image", {"resource_uuid": "abc", "attachment_id": 1, "image_id": 2}),
        ("pin.place_cid.media", {"cid": 1, "media_id": 2}),
    )

    def test_every_proxy_route_is_guarded(self) -> None:
        from urbanlens.dashboard.controllers.pin import REDATA_MEDIA_RATE

        for name, kwargs in self.ROUTES:
            with self.subTest(name):
                view = resolve(reverse(name, kwargs=kwargs)).func
                self.assertEqual(
                    getattr(view, "throttle_scope", None), "redata.media", f"{name} is not behind a throttle"
                )
                self.assertEqual(getattr(view, "throttle_rate", None), REDATA_MEDIA_RATE)

    def test_a_get_is_counted(self) -> None:
        """`throttled` counted only unsafe methods, which for a download proxy
        is every method but the one that matters."""
        for name, kwargs in self.ROUTES:
            with self.subTest(name):
                self.assertIn(
                    "GET", getattr(resolve(reverse(name, kwargs=kwargs)).func, "throttle_methods", frozenset())
                )

    def test_the_rate_is_generous_enough_for_a_gallery(self) -> None:
        """A page of photos is many requests. A limit tight enough to break
        ordinary browsing would be reverted, not tuned."""
        from urbanlens.dashboard.controllers.pin import REDATA_MEDIA_RATE

        self.assertGreaterEqual(REDATA_MEDIA_RATE.limit, 300)


class TheDecoratorCanCountSafeMethodsTests(TestCase):
    """The mechanism the above rests on, tested where it lives."""

    def test_the_default_still_ignores_a_get(self) -> None:
        """Throttling every GET by default would put a limiter in front of every
        page on the site."""
        self.assertNotIn("GET", throttle.COUNTED_METHODS)

    def test_a_caller_can_ask_for_get_to_be_counted(self) -> None:
        calls = []

        @throttle.throttled("probe.scope", throttle.Rate(limit=1, window_seconds=60), methods=frozenset({"GET"}))
        def view(request):  # noqa: ANN001, ANN202
            calls.append(1)
            from django.http import HttpResponse

            return HttpResponse("ok")

        from django.test import RequestFactory

        factory = RequestFactory()
        self.assertEqual(view(factory.get("/probe/")).status_code, 200)
        self.assertEqual(view(factory.get("/probe/")).status_code, 429)
        self.assertEqual(len(calls), 1)
