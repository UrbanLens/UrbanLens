"""Tests for background draft-wiki auto-creation and keeping it invisible until claimed.

Covers the ``Wiki.officially_created`` feature end-to-end: the ``Pin``
post_save signal that queues ``tasks.ensure_draft_wiki_for_location``, the
task itself, ``WikiCreationService.create_for_pin`` promoting a pre-existing
draft, and that a draft stays invisible through ``resolve_visible_wiki`` and
global search until it's promoted.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.wiki.wiki_creation import WikiCreationService

_SAFELY_ENQUEUE = "urbanlens.dashboard.services.core.celery.safely_enqueue_task"


class PinSignalQueuesDraftWikiTests(TestCase):
    """The Pin post_save signal queues ensure_draft_wiki_for_location correctly.

    The signal enqueues on_commit (like every other Celery-enqueuing signal
    in models.pin.signals), so a plain TestCase transaction - which rolls
    back rather than commits - never fires it on its own; wrap the
    pin-creating call in ``captureOnCommitCallbacks(execute=True)`` wherever
    the enqueue matters, matching this codebase's existing convention (see
    e.g. test_label_map_pin_cache_signal.py).
    """

    def setUp(self) -> None:
        self.profile = baker.make(User).profile

    def test_creating_a_pin_with_a_location_enqueues_the_task(self) -> None:
        location = baker.make(Location)
        with patch(_SAFELY_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile, location=location)
        from urbanlens.dashboard.tasks import ensure_draft_wiki_for_location

        enqueue.assert_any_call(ensure_draft_wiki_for_location, location.pk)

    def test_editing_an_existing_pin_does_not_re_enqueue(self) -> None:
        location = baker.make(Location)
        pin = baker.make(Pin, profile=self.profile, location=location)
        with patch(_SAFELY_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            pin.name = "Renamed"
            pin.save(update_fields=["name", "updated"])
        enqueue.assert_not_called()

    def test_pin_with_no_location_does_not_enqueue(self) -> None:
        """Pin.location is non-nullable, so this exercises the guard directly
        against an unsaved instance rather than via an invalid fixture."""
        from urbanlens.dashboard.models.pin.signals import ensure_draft_wiki_for_pin_location

        pin = Pin(profile=self.profile)
        with patch(_SAFELY_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            ensure_draft_wiki_for_pin_location(Pin, pin, created=True)
        enqueue.assert_not_called()

    def test_community_disabled_profile_does_not_enqueue(self) -> None:
        self.profile.community_enabled = False
        self.profile.save(update_fields=["community_enabled"])
        location = baker.make(Location)
        with patch(_SAFELY_ENQUEUE) as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile, location=location)
        enqueue.assert_not_called()


class EnsureDraftWikiForLocationTaskTests(TestCase):
    """tasks.ensure_draft_wiki_for_location: creates a draft, enqueues enrichment once."""

    def test_creates_a_draft_and_enqueues_enrichment(self) -> None:
        from urbanlens.dashboard import tasks

        location = baker.make(Location, official_name="Old Mill")
        with patch(_SAFELY_ENQUEUE) as enqueue:
            wiki_id = tasks.ensure_draft_wiki_for_location(location.pk)

        wiki = Wiki.objects.get(pk=wiki_id)
        self.assertFalse(wiki.officially_created)
        self.assertEqual(wiki.name, "Old Mill")
        enqueue.assert_called_once_with(tasks.enrich_wiki_location, wiki.pk)

    def test_does_not_re_enqueue_for_an_already_existing_wiki(self) -> None:
        from urbanlens.dashboard import tasks

        location = baker.make(Location)
        existing = Wiki.objects.create(location=location, name="Official")
        with patch(_SAFELY_ENQUEUE) as enqueue:
            wiki_id = tasks.ensure_draft_wiki_for_location(location.pk)

        self.assertEqual(wiki_id, existing.pk)
        enqueue.assert_not_called()

    def test_missing_location_is_a_no_op(self) -> None:
        from urbanlens.dashboard import tasks

        with patch(_SAFELY_ENQUEUE) as enqueue:
            result = tasks.ensure_draft_wiki_for_location(999_999_999)
        self.assertIsNone(result)
        enqueue.assert_not_called()


class CreateForPinPromotesExistingDraftTests(TestCase):
    """WikiCreationService.create_for_pin against a pre-existing background draft."""

    def setUp(self) -> None:
        self.profile = baker.make(User).profile
        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, danger=4)
        self.draft, _ = Wiki.objects.get_or_create_draft_for_location(self.location)

    def test_promotes_the_draft_instead_of_creating_a_second_row(self) -> None:
        with patch(_SAFELY_ENQUEUE):
            wiki, newly_official = WikiCreationService().create_for_pin(self.pin, include_fields={"danger"})

        self.assertTrue(newly_official)
        self.assertEqual(wiki.pk, self.draft.pk)
        self.assertTrue(wiki.officially_created)
        self.assertEqual(wiki.created_by_id, self.profile.pk)

    def test_seeds_chosen_pin_fields_onto_the_promoted_draft(self) -> None:
        from urbanlens.dashboard.models.wiki_stat_vote.model import WikiStatVote

        with patch(_SAFELY_ENQUEUE):
            wiki, _newly_official = WikiCreationService().create_for_pin(self.pin, include_fields={"danger"})

        vote = WikiStatVote.objects.get(wiki=wiki, profile=self.profile, field="danger")
        self.assertEqual(vote.value, 4)


class DraftWikiStaysInvisibleTests(TestCase):
    """A draft-only wiki is indistinguishable from "no wiki" everywhere a user can observe it."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.location = baker.make(Location)
        self.pin = baker.make(Pin, profile=self.user.profile, location=self.location)
        self.draft, _ = Wiki.objects.get_or_create_draft_for_location(self.location)
        self.client.force_login(self.user)

    def test_wiki_page_404s_for_a_draft_even_when_pinned(self) -> None:
        response = self.client.get(reverse("location.wiki", args=[self.location.slug]))
        self.assertEqual(response.status_code, 404)

    def test_wiki_page_succeeds_once_promoted(self) -> None:
        Wiki.objects.filter(pk=self.draft.pk).update(officially_created=True)
        response = self.client.get(reverse("location.wiki", args=[self.location.slug]))
        self.assertEqual(response.status_code, 200)

    def test_global_search_excludes_a_draft_wiki(self) -> None:
        self.draft.name = "Secret Draft Mill"
        self.draft.save(update_fields=["name"])
        response = GlobalSearchEngine().search(self.user.profile, "secret draft mill")
        titles = [r.title for g in response.groups if g.meta.slug == "wikis" for r in g.results]
        self.assertNotIn("Secret Draft Mill", titles)

    def test_global_search_finds_the_wiki_once_promoted(self) -> None:
        self.draft.name = "Promoted Mill"
        self.draft.officially_created = True
        self.draft.save(update_fields=["name", "officially_created"])
        response = GlobalSearchEngine().search(self.user.profile, "promoted mill")
        titles = [r.title for g in response.groups if g.meta.slug == "wikis" for r in g.results]
        self.assertIn("Promoted Mill", titles)

    def test_global_search_excludes_a_draft_wikis_article(self) -> None:
        from urbanlens.dashboard.models.article.model import Article

        baker.make(Article, wiki=self.draft, pin=None, content="A rare unusual keyword here.")
        response = GlobalSearchEngine().search(self.user.profile, "unusual keyword")
        titles = [r.title for g in response.groups if g.meta.slug == "articles" for r in g.results]
        self.assertEqual(titles, [])
