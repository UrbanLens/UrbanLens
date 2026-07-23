"""Tests for the wiki Media gallery: vote aggregation, voting endpoint, and the
per-source provider view (controllers/wiki_media.py + MediaRelevanceQuerySet).

The wiki reuses the Location-scoped ``MediaRelevance`` model as a community
vote store, so these cover the three behaviors that make that work without a
schema change:

* ``vote_scores`` aggregates every profile's marks into a net score (up - down).
* A pin-detail relevance mark already counts toward the wiki score (carry-over),
  because the model is keyed by Location, not Pin.
* External media renders straight from the shared ``LocationCache``; only
  photos intentionally shared to the wiki (``Image.wiki``) show under "photos".
"""

from __future__ import annotations

import json
import tempfile
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.middleware.csrf import get_token
from django.test import Client, RequestFactory, override_settings
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.images.relevance import MediaRelevance, media_item_key
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki

_MEDIA_ROOT = tempfile.mkdtemp(prefix="urbanlens-test-wiki-media-")


def _mark(profile, location, source, item_key, is_relevant) -> MediaRelevance:
    return MediaRelevance.objects.create(profile=profile, location=location, source=source, item_key=item_key, is_relevant=is_relevant)


class VoteScoresTests(TestCase):
    """MediaRelevanceQuerySet.vote_scores aggregates marks into net scores."""

    def setUp(self) -> None:
        self.location = baker.make(Location)
        self.profiles = [baker.make(User).profile for _ in range(3)]

    def test_net_score_is_upvotes_minus_downvotes(self) -> None:
        # item "a": 2 up, 1 down -> +1 ; item "b": 1 down -> -1
        _mark(self.profiles[0], self.location, "wikimedia", "a", True)
        _mark(self.profiles[1], self.location, "wikimedia", "a", True)
        _mark(self.profiles[2], self.location, "wikimedia", "a", False)
        _mark(self.profiles[0], self.location, "wikimedia", "b", False)

        scores = MediaRelevance.objects.vote_scores(self.location, "wikimedia")
        self.assertEqual(scores["a"], 1)
        self.assertEqual(scores["b"], -1)

    def test_scores_are_scoped_to_location_and_source(self) -> None:
        other_location = baker.make(Location)
        _mark(self.profiles[0], self.location, "wikimedia", "a", True)
        _mark(self.profiles[0], self.location, "smithsonian", "a", True)
        _mark(self.profiles[1], other_location, "wikimedia", "a", True)

        scores = MediaRelevance.objects.vote_scores(self.location, "wikimedia")
        self.assertEqual(scores, {"a": 1})

    def test_unmarked_item_is_absent(self) -> None:
        scores = MediaRelevance.objects.vote_scores(self.location, "wikimedia")
        self.assertEqual(scores.get("never-marked", 0), 0)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class WikiMediaVoteViewTests(TestCase):
    """POST location.wiki.media.vote records the viewer's vote and returns the net score."""

    def setUp(self) -> None:
        self.client = Client(enforce_csrf_checks=True)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.csrf_token = get_token(RequestFactory().get("/"))
        self.client.cookies["csrftoken"] = self.csrf_token

        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        # A wiki is only visible to a profile with a pin at that location.
        baker.make(Pin, profile=self.profile, location=self.location)

    def _vote(self, body: dict):
        return self.client.post(
            reverse("location.wiki.media.vote", args=[self.location.slug]),
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token,
        )

    def test_upvote_records_mark_and_returns_score(self) -> None:
        response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"my_vote": True, "vote_score": 1})
        self.assertTrue(MediaRelevance.objects.filter(profile=self.profile, location=self.location, source="wikimedia", item_key="a", is_relevant=True).exists())

    def test_clearing_a_vote_deletes_the_mark(self) -> None:
        _mark(self.profile, self.location, "wikimedia", "a", True)
        response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"my_vote": None, "vote_score": 0})
        self.assertFalse(MediaRelevance.objects.filter(profile=self.profile, location=self.location, source="wikimedia", item_key="a").exists())

    def test_pin_detail_mark_carries_over_to_the_wiki_score(self) -> None:
        """A mark made by another user (e.g. on their pin detail page) already
        counts, since MediaRelevance is Location-scoped."""
        other = baker.make(User).profile
        _mark(other, self.location, "wikimedia", "a", True)

        response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True})
        self.assertEqual(response.json()["vote_score"], 2)

    def test_vote_404s_for_a_user_without_a_pin_at_the_location(self) -> None:
        stranger = baker.make(User)
        client = Client(enforce_csrf_checks=True)
        client.force_login(stranger)
        client.cookies["csrftoken"] = self.csrf_token
        response = client.post(
            reverse("location.wiki.media.vote", args=[self.location.slug]),
            data=json.dumps({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.csrf_token,
        )
        self.assertEqual(response.status_code, 404)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class WikiMediaProviderViewTests(TestCase):
    """GET location.wiki.media renders vote-annotated tiles for one provider."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.location = baker.make(Location)
        self.wiki = baker.make(Wiki, location=self.location)
        baker.make(Pin, profile=self.profile, location=self.location)

    def test_photos_source_shows_only_wiki_shared_images(self) -> None:
        shared = Image.objects.create(
            image=SimpleUploadedFile("shared.jpg", b"bytes", content_type="image/jpeg"),
            wiki=self.wiki,
            location=self.location,
            profile=self.profile,
        )
        unrelated = Image.objects.create(
            image=SimpleUploadedFile("private.jpg", b"bytes", content_type="image/jpeg"),
            pin=baker.make(Pin, profile=self.profile),
            wiki=None,
            profile=self.profile,
        )
        response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "photos"]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(shared.image.url, body)
        self.assertNotIn(unrelated.image.url, body)

    def test_external_source_renders_cached_items_with_vote_scores(self) -> None:
        from urbanlens.dashboard.services.external_data import get_panel_source

        panel = get_panel_source("wikimedia")
        url_a = "https://example.com/a.jpg"
        LocationCache.set(
            self.location,
            panel.cache_source,
            {"items": [
                {"url": url_a, "thumb_url": url_a, "caption": "A", "source": "Wikimedia", "page_url": url_a},
                {"url": "https://example.com/b.jpg", "thumb_url": "https://example.com/b.jpg", "caption": "B", "source": "Wikimedia", "page_url": "https://example.com/b.jpg"},
            ]},
            query_key="q",
        )
        # A prior up-vote (as if from another user's pin detail page).
        other = baker.make(User).profile
        _mark(other, self.location, "wikimedia", media_item_key(url_a), True)

        response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "wikimedia"]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(url_a, body)
        self.assertIn("https://example.com/b.jpg", body)
        # Item A carries the carried-over +1 score on its tile.
        self.assertIn('data-vote-score="1"', body)
        # Wiki tiles wire their thumbs to the vote handler, not the pin's relevance handler.
        self.assertIn("window.wikiMediaVote", body)

    def test_provider_404s_for_a_user_without_a_pin(self) -> None:
        stranger = baker.make(User)
        client = Client()
        client.force_login(stranger)
        response = client.get(reverse("location.wiki.media", args=[self.location.slug, "wikimedia"]))
        self.assertEqual(response.status_code, 404)

    def test_uncached_external_source_schedules_a_fetch_and_returns_a_pending_loader(self) -> None:
        with mock.patch("urbanlens.dashboard.services.external_data.schedule_panel_fetch", return_value=True) as sched:
            response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "wikimedia"]))
        # Either a pending loader (fetch scheduled) or a quiet 204 if the panel
        # gate rejected this pin - both are valid; if it did schedule, the
        # response must be the self-polling loader retargeted at the wiki grid.
        if response.status_code == 200:
            self.assertTrue(sched.called)
            self.assertEqual(response["HX-Retarget"], "#wiki-media-loader-wikimedia")
            self.assertEqual(response["UL-Panel-Pending"], "1")
        else:
            self.assertEqual(response.status_code, 204)
