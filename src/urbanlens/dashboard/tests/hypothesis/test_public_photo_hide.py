"""The "Remove from my results" action hides a public-source photo from one account's pin pages and nowhere else (P233)."""

from __future__ import annotations

import json

from django.db import IntegrityError, transaction
from django.urls import reverse
from model_bakery import baker

from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.tests.hypothesis.test_pin_photos_tab_is_comprehensive import (
    PinPhotosTabTestCase,
    _wikimedia_item,
)

HIDDEN = _wikimedia_item(1)
SHOWN = _wikimedia_item(2)


class _HideCase(PinPhotosTabTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._cache("wikimedia", [HIDDEN, SHOWN])

    def post(self, body: dict, pin=None):
        pin = pin or self.pin
        response = self.client.post(
            reverse("pin.media.relevance", args=[pin.slug]),
            data=json.dumps({"source": "wikimedia", "url": HIDDEN["url"], **body}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def listed(self) -> list[str]:
        return [entry["url"] for entry in self._all("external")]

    def score(self) -> int:
        return MediaRelevance.objects.vote_scores(self.pin.location, "wikimedia").get(media_item_key(HIDDEN["url"]), 0)


class HideTests(_HideCase):
    def test_a_hidden_photo_leaves_the_owners_photos_tab(self) -> None:
        self.assertEqual(self.post({"hidden": True}), {"hidden": True})

        self.assertEqual(self.listed(), [SHOWN["url"]])

    def test_the_shared_row_is_untouched(self) -> None:
        before = LocationCache.objects.get(location=self.pin.location, source="wikimedia")

        self.post({"hidden": True})

        after = LocationCache.objects.get(pk=before.pk)
        self.assertEqual((after.data, after.updated), (before.data, before.updated))

    def test_another_account_still_sees_it(self) -> None:
        other = baker.make_recipe("dashboard.pin", name="Hudson River State Hospital", location=self.pin.location)

        self.post({"hidden": True})

        self.client.force_login(other.profile.user)
        self.pin = other
        self.assertEqual(self.listed(), [HIDDEN["url"], SHOWN["url"]])

    def test_it_is_hidden_whichever_provider_returns_it(self) -> None:
        self._cache("wikipedia_media", [_wikimedia_item(1, source="Wikipedia")])

        self.post({"hidden": True})

        self.assertEqual(self.listed(), [SHOWN["url"]])

    def test_unhiding_brings_it_back(self) -> None:
        self.post({"hidden": True})

        self.assertEqual(self.post({"hidden": False}), {"hidden": False})

        self.assertEqual(self.listed(), [HIDDEN["url"], SHOWN["url"]])


class MalformedKeyTests(_HideCase):
    def test_a_key_that_is_not_a_media_key_is_refused_not_a_server_error(self) -> None:
        url = reverse("pin.media.relevance", args=[self.pin.slug])
        for body in ({"hidden": True, "item_key": "f" * 41}, {"is_relevant": False, "item_key": ["f" * 40]}):
            with self.subTest(body=body):
                response = self.client.post(
                    url,
                    data=json.dumps({"source": "wikimedia", "url": HIDDEN["url"], **body}),
                    content_type="application/json",
                )

                self.assertEqual(response.status_code, 400)
        self.assertFalse(MediaRelevance.objects.exists())

    def test_the_wiki_refuses_one_too(self) -> None:
        baker.make(Wiki, location=self.pin.location, name="Hudson River State Hospital")

        response = self.client.post(
            reverse("location.wiki.media.vote", args=[self.pin.location.slug]),
            data=json.dumps({"source": "wikimedia", "url": HIDDEN["url"], "item_key": "f" * 41, "is_relevant": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(MediaRelevance.objects.exists())


class NotAVoteTests(_HideCase):
    def test_a_hide_counts_toward_no_score(self) -> None:
        self.post({"hidden": True})

        self.assertEqual(self.score(), 0)

    def test_a_hide_withdraws_the_accounts_own_vote(self) -> None:
        self.post({"is_relevant": False})
        self.assertEqual(self.score(), -1)

        self.post({"hidden": True})

        self.assertEqual(self.score(), 0)

    def test_a_vote_after_a_hide_is_a_vote(self) -> None:
        self.post({"hidden": True})

        self.post({"is_relevant": False})

        self.assertEqual(self.score(), -1)

    def test_unhiding_leaves_a_vote_alone(self) -> None:
        self.post({"is_relevant": False})

        self.post({"hidden": False})

        self.assertEqual(self.score(), -1)

    def test_the_wiki_shows_no_vote_from_the_account_that_hid_it(self) -> None:
        baker.make(Wiki, location=self.pin.location, name="Hudson River State Hospital")
        self.post({"hidden": True})

        response = self.client.get(reverse("location.wiki.media", args=[self.pin.location.slug, "wikimedia"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-media-key="{media_item_key(HIDDEN["url"])}"')
        self.assertNotContains(response, 'data-media-relevant="false"')

    def test_a_private_mark_cannot_say_relevant(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            MediaRelevance.objects.create(
                profile=self.profile,
                location=self.pin.location,
                source="wikimedia",
                item_key="a" * 40,
                is_relevant=True,
                is_vote=False,
            )


class LightboxTests(_HideCase):
    def test_the_pin_pages_lightbox_offers_it(self) -> None:
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))

        self.assertContains(response, 'data-lightbox-action="hide"')
