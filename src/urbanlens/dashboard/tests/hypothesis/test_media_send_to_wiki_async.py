"""Sending gallery media to a wiki must not download inside the request.

A full selection is up to 20 remote fetches. Done inline that is a multi-second
hang with no progress indicator, against a project standard that says anything
non-instant shows one; worse, a request that times out partway attaches some
photos and silently drops the rest, with the user's toast reporting success.

The split mirrors ``cache_media_item_into_album``: validate and enqueue in the
request, download in the task.
"""

from __future__ import annotations

import json
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"
_MATERIALIZE = "urbanlens.dashboard.services.media.media_materialize.materialize_media_item"


def _item(url: str) -> dict:
    return {"source": "wikimedia", "url": url, "page_url": f"{url}/page", "caption": "A caption"}


class MediaSendToWikiTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = Location.objects.create(latitude=40.0, longitude=-74.0)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location)
        self.wiki = baker.make(Wiki, location=self.location, name="Powerhouse")
        self.url = reverse("pin.media.send_to_wiki", args=[self.pin.slug])

    def _post(self, items: list[dict]):
        return self.client.post(self.url, data=json.dumps({"items": items}), content_type="application/json")

    def test_nothing_is_downloaded_inside_the_request(self) -> None:
        with mock.patch(_MATERIALIZE) as materialize, mock.patch(_ENQUEUE) as enqueue:
            response = self._post([_item("https://example.test/a.jpg"), _item("https://example.test/b.jpg")])

        self.assertEqual(response.status_code, 200)
        materialize.assert_not_called()
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(response.json()["queued"], 2)

    def test_each_queued_item_carries_its_own_details(self) -> None:
        with mock.patch(_MATERIALIZE), mock.patch(_ENQUEUE) as enqueue:
            self._post([_item("https://example.test/a.jpg")])

        _task, wiki_id, profile_id, source, url, page_url, caption = enqueue.call_args.args
        self.assertEqual(wiki_id, self.wiki.pk)
        self.assertEqual(profile_id, self.profile.pk)
        self.assertEqual(source, "wikimedia")
        self.assertEqual(url, "https://example.test/a.jpg")
        self.assertEqual(page_url, "https://example.test/a.jpg/page")
        self.assertEqual(caption, "A caption")

    def test_the_twenty_item_cap_still_holds(self) -> None:
        with mock.patch(_MATERIALIZE), mock.patch(_ENQUEUE) as enqueue:
            response = self._post([_item(f"https://example.test/{n}.jpg") for n in range(25)])

        self.assertEqual(enqueue.call_count, 20)
        self.assertEqual(response.json()["queued"], 20)

    def test_a_malformed_entry_is_reported_without_stopping_the_rest(self) -> None:
        with mock.patch(_MATERIALIZE), mock.patch(_ENQUEUE) as enqueue:
            response = self._post([{"source": "wikimedia"}, _item("https://example.test/b.jpg")])

        self.assertEqual(enqueue.call_count, 1)
        payload = response.json()
        self.assertEqual(payload["queued"], 1)
        self.assertEqual(len(payload["errors"]), 1)

    def test_a_location_with_no_wiki_is_still_rejected_up_front(self) -> None:
        other_location = Location.objects.create(latitude=41.0, longitude=-75.0)
        other_pin = baker.make(Pin, profile=self.profile, location=other_location)

        with mock.patch(_ENQUEUE) as enqueue:
            response = self.client.post(
                reverse("pin.media.send_to_wiki", args=[other_pin.slug]),
                data=json.dumps({"items": [_item("https://example.test/a.jpg")]}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 400)
        enqueue.assert_not_called()


class CacheMediaItemIntoWikiTaskTests(TestCase):
    """The task owns the download, and must tolerate its subject disappearing."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = Location.objects.create(latitude=42.0, longitude=-76.0)
        self.wiki = baker.make(Wiki, location=self.location, name="Boiler House")

    def test_it_materializes_against_the_wikis_location(self) -> None:
        from urbanlens.dashboard.tasks import cache_media_item_into_wiki

        with mock.patch(_MATERIALIZE) as materialize:
            materialize.return_value = mock.Mock(pk=7)
            result = cache_media_item_into_wiki(
                self.wiki.pk, self.profile.pk, "wikimedia", "https://example.test/a.jpg"
            )

        self.assertEqual(result, 7)
        self.assertEqual(materialize.call_args.kwargs["location"], self.location)
        self.assertEqual(materialize.call_args.kwargs["wiki"], self.wiki)

    def test_a_deleted_wiki_is_a_no_op_rather_than_an_error(self) -> None:
        from urbanlens.dashboard.tasks import cache_media_item_into_wiki

        missing_pk = self.wiki.pk
        self.wiki.delete()

        with mock.patch(_MATERIALIZE) as materialize:
            result = cache_media_item_into_wiki(missing_pk, self.profile.pk, "wikimedia", "https://example.test/a.jpg")

        self.assertIsNone(result)
        materialize.assert_not_called()

    def test_a_failed_download_returns_none_instead_of_raising(self) -> None:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError
        from urbanlens.dashboard.tasks import cache_media_item_into_wiki

        with mock.patch(_MATERIALIZE, side_effect=MaterializeError("dead provider")):
            result = cache_media_item_into_wiki(
                self.wiki.pk, self.profile.pk, "wikimedia", "https://example.test/a.jpg"
            )

        self.assertIsNone(result)
