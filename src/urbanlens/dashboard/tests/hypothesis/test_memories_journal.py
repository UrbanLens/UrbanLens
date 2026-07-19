"""Tests for the Memories "Journal" subpage.

Covers ``get_journal_entries`` (the service that merges a profile's own
visit notes, pin ratings, comments, and article edits into one newest-first
feed) and ``MemoriesJournalView`` (the page that renders it).
"""

from __future__ import annotations

import datetime
import itertools

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.memories.journal import get_journal_entries

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _aware(year: int, month: int, day: int) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime(year, month, day, 12, 0, 0))


def _make_pin(profile, *, name=None):
    """Create a test pin with a uniquely-located Location to dodge the unique constraint."""
    offset = next(_COORDS)
    location = baker.make("dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}")
    return baker.make("dashboard.Pin", profile=profile, location=location, name=name)


class GetJournalEntriesTests(TestCase):
    """get_journal_entries() scopes to the profile and merges sources newest-first."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        self.other = baker.make(User).profile

    def test_visit_with_notes_is_included(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), notes="Rusty catwalks everywhere.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "visit")
        self.assertEqual(entries[0].title, "Old Factory")
        self.assertEqual(entries[0].body, "Rusty catwalks everywhere.")

    def test_visit_without_notes_is_excluded(self) -> None:
        pin = _make_pin(self.profile)
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), notes=None)
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 2), notes="")

        self.assertEqual(get_journal_entries(self.profile), [])

    def test_review_is_included_with_rating(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=4)

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "review")
        self.assertEqual(entries[0].rating, 4)
        self.assertEqual(entries[0].title, "Old Factory")

    def test_review_has_no_redundant_rating_subtitle(self) -> None:
        """Regression guard: the star row itself already conveys "this is a
        rating" - a "Rating" text label next to it was pure redundancy."""
        pin = _make_pin(self.profile, name="Old Factory")
        baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=4)

        entries = get_journal_entries(self.profile)

        self.assertEqual(entries[0].subtitle, "")

    def test_pin_comment_links_to_pin_detail(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        baker.make("dashboard.Comment", profile=self.profile, pin=pin, parent=None, text="Watch the third floor.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "comment")
        self.assertEqual(entries[0].body, "Watch the third floor.")
        self.assertEqual(entries[0].url, reverse("pin.details", kwargs={"pin_slug": pin.slug}) + "#tab-comments")

    def test_wiki_comment_links_to_wiki(self) -> None:
        location = baker.make("dashboard.Location", latitude=42.0, longitude=-75.0)
        wiki = baker.make("dashboard.Wiki", location=location, name="Old Factory Wiki", slug="old-factory-wiki")
        baker.make("dashboard.Comment", profile=self.profile, wiki=wiki, parent=None, text="Community note.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Old Factory Wiki")
        self.assertEqual(entries[0].url, reverse("location.wiki", kwargs={"location_slug": location.slug}) + "#tab-comments")

    def test_trip_comment_links_to_trip(self) -> None:
        trip = baker.make("dashboard.Trip", creator=self.profile, name="Fall Roadtrip")
        baker.make("dashboard.TripComment", trip=trip, author=self.profile, parent=None, text="Bring boots.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Fall Roadtrip")
        self.assertEqual(entries[0].url, reverse("trips.detail", kwargs={"trip_slug": trip.slug}) + "#trip-comments-panel")

    def test_pin_article_edit_links_to_pin_detail(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        article = baker.make("dashboard.Article", pin=pin)
        baker.make("dashboard.ArticleRevision", article=article, editor=self.profile, content="Built in 1912.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "article")
        self.assertEqual(entries[0].title, "Old Factory")
        self.assertEqual(entries[0].subtitle, "Article edit")
        self.assertEqual(entries[0].body, "Built in 1912.")
        self.assertEqual(entries[0].url, reverse("pin.details", kwargs={"pin_slug": pin.slug}) + "#tab-article")

    def test_wiki_article_edit_links_to_wiki(self) -> None:
        location = baker.make("dashboard.Location", latitude=44.0, longitude=-77.0)
        wiki = baker.make("dashboard.Wiki", location=location, name="Old Factory Wiki", slug="old-factory-wiki")
        article = baker.make("dashboard.Article", wiki=wiki)
        baker.make("dashboard.ArticleRevision", article=article, editor=self.profile, content="Community history.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Old Factory Wiki")
        self.assertEqual(entries[0].subtitle, "Wiki article edit")
        self.assertEqual(entries[0].url, reverse("location.wiki", kwargs={"location_slug": location.slug}) + "#tab-article")

    def test_article_edit_prefers_edit_summary_for_body(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        article = baker.make("dashboard.Article", pin=pin)
        baker.make(
            "dashboard.ArticleRevision",
            article=article,
            editor=self.profile,
            content="Built in 1912.",
            edit_summary="Added construction date",
        )

        entries = get_journal_entries(self.profile)

        self.assertEqual(entries[0].body, "Added construction date")

    def test_article_edit_by_other_profile_is_excluded(self) -> None:
        pin = _make_pin(self.other, name="Theirs")
        article = baker.make("dashboard.Article", pin=pin)
        baker.make("dashboard.ArticleRevision", article=article, editor=self.other, content="Not mine.")

        self.assertEqual(get_journal_entries(self.profile), [])

    def test_only_returns_owning_profiles_entries(self) -> None:
        mine = _make_pin(self.profile, name="Mine")
        theirs = _make_pin(self.other, name="Theirs")
        PinVisit.objects.create(pin=mine, visited_at=_aware(2024, 6, 1), notes="Mine.")
        PinVisit.objects.create(pin=theirs, visited_at=_aware(2024, 6, 1), notes="Theirs.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Mine")

    def test_sorted_newest_first_across_mixed_kinds(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        older_visit = PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 1, 1), notes="First trip.")
        review = baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=5)
        review.created = _aware(2024, 6, 1)
        review.save(update_fields=["created"])
        newer_visit = PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 12, 1), notes="Back again.")

        entries = get_journal_entries(self.profile)

        self.assertEqual([e.kind for e in entries], ["visit", "review", "visit"])
        self.assertEqual(entries[0].body, newer_visit.notes)
        self.assertEqual(entries[2].body, older_visit.notes)


class MemoriesJournalViewTests(TestCase):
    """MemoriesJournalView (the Journal subpage) renders the merged feed."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("memories.journal"))
        self.assertEqual(response.status_code, 302)

    def test_empty_state_shown_when_no_entries(self) -> None:
        response = self.client.get(reverse("memories.journal"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["journal_entries"], [])
        self.assertContains(response, "Your journal is empty.")

    def test_lists_visit_note_and_rating(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), notes="Rusty catwalks everywhere.")
        baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=4)

        response = self.client.get(reverse("memories.journal"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["journal_entries"]), 2)
        self.assertContains(response, "Old Factory")
        self.assertContains(response, "Rusty catwalks everywhere.")

    def test_rating_entry_does_not_render_a_redundant_rating_label(self) -> None:
        pin = _make_pin(self.profile, name="Old Factory")
        baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=4)

        response = self.client.get(reverse("memories.journal"))

        self.assertNotContains(response, '<span class="memories-journal-subtitle">Rating</span>')
