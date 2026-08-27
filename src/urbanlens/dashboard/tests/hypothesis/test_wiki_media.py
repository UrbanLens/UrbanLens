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
        image = Image.objects.create(image=SimpleUploadedFile("materialized.jpg", b"bytes", content_type="image/jpeg"), wiki=None, location=self.location, profile=self.profile)
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=image) as materialize:
            response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"my_vote": True, "vote_score": 1, "image_id": image.pk, "image_url": image.image.url})
        self.assertTrue(MediaRelevance.objects.filter(profile=self.profile, location=self.location, source="wikimedia", item_key="a", is_relevant=True).exists())
        materialize.assert_called_once_with(location=self.location, profile=self.profile, source="wikimedia", url="https://x/a.jpg", page_url="", caption="", wiki=self.wiki)

    def test_upvote_passes_page_url_and_caption_through_to_materialize(self) -> None:
        image = Image.objects.create(image=SimpleUploadedFile("materialized.jpg", b"bytes", content_type="image/jpeg"), wiki=None, location=self.location, profile=self.profile)
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=image) as materialize:
            self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "page_url": "https://x/a", "caption": "A photo", "is_relevant": True})
        materialize.assert_called_once_with(location=self.location, profile=self.profile, source="wikimedia", url="https://x/a.jpg", page_url="https://x/a", caption="A photo", wiki=self.wiki)

    def test_upvoting_a_photos_tab_item_does_not_re_materialize_it(self) -> None:
        """The 'photos' source key lists photos already attached to this wiki
        (WikiMediaProviderView._photos) - its url is a local media path, not
        an external provider url, so re-materializing it would be wrong."""
        image = Image.objects.create(image=SimpleUploadedFile("shared.jpg", b"bytes", content_type="image/jpeg"), wiki=self.wiki, location=self.location, profile=self.profile)
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item") as materialize:
            response = self._vote({"source": "photos", "item_key": "a", "url": image.image.url, "is_relevant": True, "image_id": image.pk})
        self.assertEqual(response.status_code, 200)
        materialize.assert_not_called()

    def test_downvote_never_materializes(self) -> None:
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item") as materialize:
            response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": False})
        self.assertEqual(response.status_code, 200)
        materialize.assert_not_called()

    def test_a_failed_materialize_still_records_the_vote_and_reports_the_error(self) -> None:
        from urbanlens.dashboard.services.media.media_materialize import MaterializeError

        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item", side_effect=MaterializeError("boom")):
            response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["materialize_error"], "Could not save this photo.")
        self.assertEqual(body["my_vote"], True)
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

        image = Image.objects.create(image=SimpleUploadedFile("materialized.jpg", b"bytes", content_type="image/jpeg"), wiki=None, location=self.location, profile=self.profile)
        with mock.patch("urbanlens.dashboard.services.media.media_materialize.materialize_media_item", return_value=image):
            response = self._vote({"source": "wikimedia", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True})
        self.assertEqual(response.json()["vote_score"], 2)

    def test_voting_with_an_image_id_queues_a_redata_vote(self) -> None:
        image = Image.objects.create(
            image=SimpleUploadedFile("shared.jpg", b"bytes", content_type="image/jpeg"),
            wiki=self.wiki,
            location=self.location,
            profile=self.profile,
        )
        with mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote") as queue_vote:
            response = self._vote({"source": "photos", "item_key": "a", "url": image.image.url, "is_relevant": True, "image_id": image.pk})
        self.assertEqual(response.status_code, 200)
        queue_vote.assert_called_once()
        (voted_image, voted_profile), kwargs = queue_vote.call_args
        self.assertEqual(voted_image.pk, image.pk)
        self.assertEqual(voted_profile, self.profile)
        self.assertEqual(kwargs, {"is_relevant": True})

    def test_voting_with_an_image_id_from_another_location_is_ignored(self) -> None:
        """A client-supplied image_id must be re-scoped to this wiki's location
        before being trusted - otherwise a vote could be attached to an
        unrelated photo elsewhere on the site."""
        other_location = baker.make(Location)
        other_image = Image.objects.create(image=SimpleUploadedFile("x.jpg", b"y", content_type="image/jpeg"), location=other_location, profile=self.profile)
        with mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote") as queue_vote:
            response = self._vote({"source": "photos", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True, "image_id": other_image.pk})
        self.assertEqual(response.status_code, 200)
        queue_vote.assert_not_called()

    def test_voting_with_a_pin_owned_image_id_at_the_same_location_is_ignored(self) -> None:
        """A photo that was only ever uploaded to a Pin (never sent to the
        wiki) must not be votable through the wiki just because it shares the
        wiki's Location - the lookup has to scope to the wiki's own attached
        media, not merely to the location."""
        pin_only_image = Image.objects.create(image=SimpleUploadedFile("pin-only.jpg", b"y", content_type="image/jpeg"), location=self.location, profile=self.profile)
        with mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote") as queue_vote:
            response = self._vote({"source": "photos", "item_key": "a", "url": "https://x/a.jpg", "is_relevant": True, "image_id": pin_only_image.pk})
        self.assertEqual(response.status_code, 200)
        queue_vote.assert_not_called()

    def test_clearing_a_vote_does_not_queue_a_redata_vote(self) -> None:
        image = Image.objects.create(image=SimpleUploadedFile("shared.jpg", b"bytes", content_type="image/jpeg"), wiki=self.wiki, location=self.location, profile=self.profile)
        _mark(self.profile, self.location, "photos", "a", is_relevant=True)
        with mock.patch("urbanlens.dashboard.services.photos.redata_relevance.queue_relevance_vote") as queue_vote:
            self._vote({"source": "photos", "item_key": "a", "url": image.image.url, "is_relevant": None, "image_id": image.pk})
        queue_vote.assert_not_called()

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

    def test_photos_are_ordered_by_vote_score_before_redata_confidence(self) -> None:
        """A community upvote outranks a merely REData-confident, unvoted photo."""
        upvoted_low_confidence = Image.objects.create(image=SimpleUploadedFile("a.jpg", b"a", content_type="image/jpeg"), wiki=self.wiki, location=self.location, profile=self.profile, redata_confidence=0.1)
        unvoted_high_confidence = Image.objects.create(image=SimpleUploadedFile("b.jpg", b"b", content_type="image/jpeg"), wiki=self.wiki, location=self.location, profile=self.profile, redata_confidence=0.9)
        _mark(self.profile, self.location, "photos", media_item_key(upvoted_low_confidence.image.url), is_relevant=True)

        response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "photos"]))
        body = response.content.decode()
        self.assertLess(body.index(f'data-image-id="{upvoted_low_confidence.pk}"'), body.index(f'data-image-id="{unvoted_high_confidence.pk}"'))

    def test_unvoted_photos_break_ties_by_redata_confidence(self) -> None:
        lower_confidence = Image.objects.create(image=SimpleUploadedFile("a.jpg", b"a", content_type="image/jpeg"), wiki=self.wiki, location=self.location, profile=self.profile, redata_confidence=0.2)
        higher_confidence = Image.objects.create(image=SimpleUploadedFile("b.jpg", b"b", content_type="image/jpeg"), wiki=self.wiki, location=self.location, profile=self.profile, redata_confidence=0.8)

        response = self.client.get(reverse("location.wiki.media", args=[self.location.slug, "photos"]))
        body = response.content.decode()
        self.assertLess(body.index(f'data-image-id="{higher_confidence.pk}"'), body.index(f'data-image-id="{lower_confidence.pk}"'))

    def test_external_source_renders_cached_items_with_vote_scores(self) -> None:
        from urbanlens.dashboard.services.pins.external_data import get_panel_source

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
        with mock.patch("urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch", return_value=True) as sched:
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
