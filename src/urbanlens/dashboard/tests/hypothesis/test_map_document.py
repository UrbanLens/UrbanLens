"""The single-document fetch, and its agreement with the paged endpoint.

The map page used to make twenty sequential requests of 500 pins to draw a
10,000-pin account. `map.document` is the same data in one response. `map.pins`
is deliberately unchanged and still serves pages - it is what a client that wants
them uses, and what this endpoint tells an over-ceiling account to fall back to.

Two properties matter more than the format:

- the two endpoints return **the same payloads**, or the fallback is a downgrade
  in correctness rather than in speed;
- the server holds one batch at a time, not the account, or the response size
  becomes worker memory.
"""

from __future__ import annotations

import gzip
import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.fake_redis import FakeRedis
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.map_pins import document as map_document


def _lines(response) -> list[dict]:
    """Decode an NDJSON response, streamed or not.

    Args:
        response: The test client's response.

    Returns:
        One decoded object per line.
    """
    body = b"".join(response.streaming_content) if response.streaming else response.content
    if response.headers.get("Content-Encoding") == "gzip":
        body = gzip.decompress(body)
    return [json.loads(line) for line in body.splitlines() if line]


class TheDocumentCarriesEveryPinTests(TestCase):
    """Shape, completeness, and the end marker that proves it is complete."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pins = [
            baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=float(i), longitude=1.0))
            for i in range(5)
        ]

    def test_it_opens_with_a_head_and_closes_with_an_end(self) -> None:
        lines = _lines(self.client.get(reverse("map.document")))

        self.assertEqual(lines[0]["t"], "head")
        self.assertEqual(lines[0]["mode"], map_document.MODE_DOCUMENT)
        self.assertEqual(lines[0]["total"], len(self.pins))
        self.assertEqual(lines[-1], {"t": "end", "sent": len(self.pins)})

    def test_it_carries_one_line_per_pin(self) -> None:
        lines = _lines(self.client.get(reverse("map.document")))

        uuids = {line["p"]["uuid"] for line in lines if line["t"] == "pin"}
        self.assertEqual(uuids, {str(pin.uuid) for pin in self.pins})

    def test_an_account_with_no_pins_still_gets_a_well_formed_document(self) -> None:
        empty = baker.make(User)
        self.client.force_login(empty)

        lines = _lines(self.client.get(reverse("map.document")))

        self.assertEqual(lines[0]["total"], 0)
        self.assertEqual(lines[-1], {"t": "end", "sent": 0})

    def test_it_is_ndjson(self) -> None:
        response = self.client.get(reverse("map.document"))

        self.assertEqual(response.headers["Content-Type"], map_document.CONTENT_TYPE)

    def test_another_account_cannot_read_it(self) -> None:
        stranger = baker.make(User)
        baker.make(Pin, profile=stranger.profile, location=baker.make(Location, latitude=80.0, longitude=9.0))
        self.client.force_login(stranger)

        lines = _lines(self.client.get(reverse("map.document")))

        self.assertEqual(lines[0]["total"], 1)
        self.assertNotIn(str(self.pins[0].uuid), {line["p"]["uuid"] for line in lines if line["t"] == "pin"})

    def test_signing_out_is_refused(self) -> None:
        self.client.logout()

        self.assertIn(self.client.get(reverse("map.document")).status_code, (301, 302))


class TheDocumentAgreesWithThePagedEndpointTests(TestCase):
    """The fallback must be slower, not different."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        for index in range(7):
            baker.make(
                Pin,
                profile=self.profile,
                location=baker.make(Location, latitude=float(index), longitude=2.0),
            )

    def _no_cache(self):
        """Run the paged endpoint against the database.

        `MapPinCache` reads its URL from the environment and the suite's network
        guard refuses the connection, so the endpoint has to be told there is no
        cache - which is also the path this comparison wants.

        Returns:
            A patch context manager.
        """
        from urbanlens.dashboard.services.map_pins import MapPinCache

        return mock.patch.object(MapPinCache, "make_client", return_value=None)

    def _paged(self) -> list[dict]:
        """Every pin, walked through `map.pins` the way the old client did.

        Returns:
            The payloads, ordered as the endpoint returned them.
        """
        pins: list[dict] = []
        cursor = None
        with self._no_cache():
            while True:
                url = reverse("map.pins") + (f"?cursor={cursor}&limit=3" if cursor else "?limit=3")
                body = self.client.get(url).json()
                pins.extend(body["pins"])
                cursor = body.get("next_cursor")
                if not cursor:
                    return pins

    def test_the_two_endpoints_return_identical_payloads(self) -> None:
        document = [line["p"] for line in _lines(self.client.get(reverse("map.document"))) if line["t"] == "pin"]

        self.assertEqual(document, self._paged())

    def test_the_paged_endpoint_still_pages(self) -> None:
        """Kept working on purpose - progressive loading needs it."""
        with self._no_cache():
            first = self.client.get(reverse("map.pins") + "?limit=3").json()

        self.assertEqual(len(first["pins"]), 3)
        self.assertIsNotNone(first["next_cursor"])


class TheDocumentDoesNotHoldTheAccountTests(TestCase):
    """Streaming, asserted rather than assumed."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        for index in range(6):
            baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=float(index), longitude=3.0))

    def test_the_whole_document_is_not_built_before_the_first_chunk(self) -> None:
        """A generator that had already run would make the streaming claim false.

        `CHUNK_BYTES` down to 1 so a chunk is as small as the format allows; at
        its real value one chunk carries the whole of a test-sized account and
        there is nothing left to observe.
        """
        with mock.patch.object(map_document, "BATCH_SIZE", 2), mock.patch.object(map_document, "CHUNK_BYTES", 1):
            response = self.client.get(reverse("map.document"))
            iterator = iter(response.streaming_content)
            first = [json.loads(line) for line in next(iterator).splitlines() if line]

            self.assertEqual(first[0]["t"], "head")
            self.assertNotIn(
                "end", [line["t"] for line in first], "the whole document was built before it was asked for"
            )

            rest = [json.loads(line) for chunk in iterator for line in chunk.splitlines() if line]

        self.assertEqual(rest[-1], {"t": "end", "sent": 6})

    def test_it_walks_the_account_in_batches(self) -> None:
        with mock.patch.object(map_document, "BATCH_SIZE", 2):
            lines = _lines(self.client.get(reverse("map.document")))

        self.assertEqual(len([line for line in lines if line["t"] == "pin"]), 6)

    def test_a_chunk_never_splits_a_line(self) -> None:
        """Chunks are an efficiency, not part of the format."""
        with mock.patch.object(map_document, "CHUNK_BYTES", 1):
            response = self.client.get(reverse("map.document"))
            chunks = list(response.streaming_content)

        for chunk in chunks:
            self.assertTrue(chunk.endswith(b"\n"))
            for line in chunk.splitlines():
                json.loads(line)


class TheCeilingFallsBackRatherThanFailingTests(TestCase):
    """An account too large for one document is told where to go instead."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        for index in range(4):
            baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=float(index), longitude=4.0))

    @override_settings(MAP_DOCUMENT_MAX_PINS=2)
    def test_over_the_ceiling_it_returns_a_head_naming_the_paged_mode(self) -> None:
        lines = _lines(self.client.get(reverse("map.document")))

        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["mode"], map_document.MODE_PAGED)
        self.assertEqual(lines[0]["total"], 4)

    @override_settings(MAP_DOCUMENT_MAX_PINS=2)
    def test_the_setting_it_reads_exists(self) -> None:
        """`override_settings` will happily invent a name production does not have."""
        from django.conf import settings

        self.assertTrue(hasattr(settings, "MAP_DOCUMENT_MAX_PINS"))
        self.assertEqual(map_document.max_pins(), 2)


class TheEtagLetsAClientSkipTheWholeThingTests(TestCase):
    """Revisiting an unchanged account should cost one 304."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=5.0, longitude=5.0))

    def test_a_matching_etag_gets_a_304(self) -> None:
        etag = self.client.get(reverse("map.document")).headers["ETag"]

        response = self.client.get(reverse("map.document"), HTTP_IF_NONE_MATCH=etag)

        self.assertEqual(response.status_code, 304)

    def test_the_etag_moves_when_a_pin_changes(self) -> None:
        before = self.client.get(reverse("map.document")).headers["ETag"]

        baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=6.0, longitude=6.0))

        self.assertNotEqual(self.client.get(reverse("map.document")).headers["ETag"], before)

    def test_the_etag_moves_when_a_pin_is_deleted(self) -> None:
        """The case `Max(updated)` alone cannot see."""
        other = baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=7.0, longitude=7.0))
        before = self.client.get(reverse("map.document")).headers["ETag"]

        self.pin.delete()

        self.assertNotEqual(self.client.get(reverse("map.document")).headers["ETag"], before)
        self.assertIsNotNone(other.pk)


class TheCacheIsAnAcceleratorTests(TestCase):
    """It must change the speed of the answer and never the answer."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.redis = FakeRedis()
        for index in range(3):
            baker.make(Pin, profile=self.profile, location=baker.make(Location, latitude=float(index), longitude=8.0))

    def test_a_missing_cache_still_serves_the_whole_document(self) -> None:
        response = self.client.get(reverse("map.document"))
        lines = _lines(response)

        self.assertEqual(response.headers["X-Map-Document"], "miss")
        self.assertEqual(lines[-1]["sent"], 3)

    def test_a_stored_document_is_served_verbatim_and_gzipped(self) -> None:
        from urbanlens.dashboard.services.map_pins.view_urls import with_view_urls

        query = Pin.objects.filter(profile=self.profile).root_pins().select_related("location")
        with mock.patch.object(map_document, "make_binary_client", return_value=self.redis):
            stored = map_document.build_and_store(self.profile, query, decorate=with_view_urls)
            self.assertGreater(stored, 0)
            response = self.client.get(reverse("map.document"))

        self.assertEqual(response.headers["X-Map-Document"], "hit")
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual([line["p"]["uuid"] for line in _lines(response) if line["t"] == "pin"], self._uuids())

    def test_the_cached_document_matches_the_streamed_one(self) -> None:
        from urbanlens.dashboard.services.map_pins.view_urls import with_view_urls

        streamed = _lines(self.client.get(reverse("map.document")))
        query = Pin.objects.filter(profile=self.profile).root_pins().select_related("location")
        with mock.patch.object(map_document, "make_binary_client", return_value=self.redis):
            map_document.build_and_store(self.profile, query, decorate=with_view_urls)
            cached = _lines(self.client.get(reverse("map.document")))

        self.assertEqual(cached, streamed)

    def test_a_second_build_does_not_overwrite_the_first(self) -> None:
        """Content-addressed, so concurrent builders write identical bytes."""
        from urbanlens.dashboard.services.map_pins.view_urls import with_view_urls

        query = Pin.objects.filter(profile=self.profile).root_pins().select_related("location")
        with mock.patch.object(map_document, "make_binary_client", return_value=self.redis):
            first = map_document.build_and_store(self.profile, query, decorate=with_view_urls)
            second = map_document.build_and_store(self.profile, query, decorate=with_view_urls)

        self.assertGreater(first, 0)
        self.assertEqual(second, 0)

    def _uuids(self) -> list[str]:
        """Every pin uuid, in the order the document emits them.

        Returns:
            Uuid strings ordered by pin pk.
        """
        return [
            str(uuid) for uuid in Pin.objects.filter(profile=self.profile).order_by("pk").values_list("uuid", flat=True)
        ]
