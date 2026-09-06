"""Tests for the Memories "Journal" subpage.

Covers ``get_journal_entries`` (the service that merges a profile's own
visit notes, pin ratings, comments, and article edits into one newest-first
feed) and ``MemoriesJournalView`` (the page that renders it).
"""

from __future__ import annotations

import datetime
import itertools

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.memories import _JOURNAL_PAGE_SIZE
from urbanlens.dashboard.models.visits.model import PinVisit
from urbanlens.dashboard.services.memories.journal import (
    JOURNAL_SOURCE_COUNTS,
    JOURNAL_SOURCES,
    JournalFeed,
    get_journal_entries,
)

# Location carries a unique (latitude, longitude) constraint, so every test pin
# needs its own coordinates.
_COORDS = itertools.count()


def _aware(year: int, month: int, day: int) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime(year, month, day, 12, 0, 0))


def _make_pin(profile, *, name=None):
    """Create a test pin with a uniquely-located Location to dodge the unique constraint."""
    offset = next(_COORDS)
    location = baker.make(
        "dashboard.Location", latitude=f"{40 + offset * 0.01:.6f}", longitude=f"{-74 + offset * 0.01:.6f}"
    )
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

    def test_visit_links_to_the_visit_history_tab(self) -> None:
        """Visit History moved from the (always-visible) Overview tab to its own
        subnav tab, which starts `hidden` - a bare #visit-history-panel anchor
        would silently do nothing now, so this must use the #tab-visits hash
        page-tabs.js recognizes to actually switch to and reveal that tab."""
        pin = _make_pin(self.profile, name="Old Factory")
        PinVisit.objects.create(pin=pin, visited_at=_aware(2024, 6, 1), notes="Rusty catwalks everywhere.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(entries[0].url, reverse("pin.details", kwargs={"pin_slug": pin.slug}) + "#tab-visits")

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
        self.assertEqual(
            entries[0].url, reverse("location.wiki", kwargs={"location_slug": location.slug}) + "#tab-comments"
        )

    def test_trip_comment_links_to_trip(self) -> None:
        trip = baker.make("dashboard.Trip", creator=self.profile, name="Fall Roadtrip")
        baker.make("dashboard.TripComment", trip=trip, author=self.profile, parent=None, text="Bring boots.")

        entries = get_journal_entries(self.profile)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Fall Roadtrip")
        self.assertEqual(
            entries[0].url, reverse("trips.detail", kwargs={"trip_slug": trip.slug}) + "#trip-comments-panel"
        )

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
        self.assertEqual(
            entries[0].url, reverse("location.wiki", kwargs={"location_slug": location.slug}) + "#tab-article"
        )

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


class JournalLimitTests(TestCase):
    """The limit that lets a page of the journal cost a page's worth of rows.

    P69: the Journal merged four unsliced querysets in Python. A limit is only
    safe here because it is *exact* - the newest N of a union can only contain
    entries that are in some source's own newest N - so these check the seam
    rather than just the count.
    """

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile

    def _visit(self, day: int, note: str) -> None:
        pin = _make_pin(self.profile)
        baker.make(PinVisit, pin=pin, visited_at=_aware(2026, 1, day), notes=note)

    def _review(self, day: int) -> None:
        pin = _make_pin(self.profile, name=f"rated-{day}")
        review = baker.make("dashboard.Review", profile=self.profile, pin=pin, rating=4)
        # `created` is auto_now_add, so it has to be written past the default.
        type(review).objects.filter(pk=review.pk).update(created=_aware(2026, 1, day))

    def test_the_limit_caps_the_merged_feed(self) -> None:
        for day in range(1, 11):
            self._visit(day, f"note {day}")

        self.assertEqual(len(get_journal_entries(self.profile, limit=4)), 4)

    def test_the_newest_survive_the_limit_whichever_source_they_came_from(self) -> None:
        # The seam: taking the newest four from each source and then merging
        # must give the same four as merging everything and taking four. A
        # per-source limit applied to the *merged* list instead would drop
        # whichever source sorted second.
        for day in (1, 2, 3, 9):
            self._visit(day, f"note {day}")
        for day in (4, 5, 6, 10):
            self._review(day)

        limited = get_journal_entries(self.profile, limit=4)
        everything = get_journal_entries(self.profile)[:4]

        self.assertEqual([entry.occurred_at for entry in limited], [entry.occurred_at for entry in everything])
        self.assertEqual({entry.kind for entry in limited}, {"visit", "review"})

    def test_a_source_yielding_nothing_does_not_shorten_the_page(self) -> None:
        for day in range(1, 8):
            self._visit(day, f"note {day}")

        self.assertEqual(len(get_journal_entries(self.profile, limit=6)), 6)


class JournalFeedTests(TestCase):
    """The sliceable sequence both the page and the external API paginate."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make(User).profile
        for day in range(1, 13):
            pin = _make_pin(self.profile)
            baker.make(PinVisit, pin=pin, visited_at=_aware(2026, 1, day), notes=f"note {day}")

    def test_its_length_is_the_whole_feed(self) -> None:
        self.assertEqual(len(JournalFeed(self.profile)), 12)

    def test_a_slice_matches_the_same_slice_of_the_full_list(self) -> None:
        feed = JournalFeed(self.profile)
        everything = get_journal_entries(self.profile)

        self.assertEqual(list(feed[0:5]), everything[0:5])
        self.assertEqual(list(feed[5:10]), everything[5:10])

    def test_a_bound_counted_from_the_end_still_matches_the_full_list(self) -> None:
        # A negative limit reaches a queryset slice, where Django raises - and
        # `get_journal_entries` catches per source, so every source vanishes
        # and the answer is a silently empty list rather than an error.
        feed = JournalFeed(self.profile)
        everything = get_journal_entries(self.profile)

        self.assertEqual(feed[-1], everything[-1])
        self.assertEqual(feed[-3], everything[-3])
        self.assertEqual(list(feed[:-1]), everything[:-1])
        self.assertEqual(list(feed[-3:-1]), everything[-3:-1])
        self.assertEqual(list(feed[-3:]), everything[-3:])

    def test_an_open_ended_or_stepped_slice_matches_too(self) -> None:
        feed = JournalFeed(self.profile)
        everything = get_journal_entries(self.profile)

        self.assertEqual(list(feed[5:]), everything[5:])
        self.assertEqual(list(feed[::2]), everything[::2])
        self.assertEqual(list(feed[0:0]), [])

    def test_it_counts_without_building_the_entries(self) -> None:
        # The point of the count is not to pay for the rows: a `len()` that
        # walked the sources would make the page's total as expensive as the
        # feed it replaced.
        with CaptureQueriesContext(connection) as queries:
            len(JournalFeed(self.profile))

        self.assertTrue(
            all("LIMIT" not in query["sql"] or "COUNT" in query["sql"] for query in queries.captured_queries),
            [q["sql"][:80] for q in queries.captured_queries],
        )
        self.assertLessEqual(len(queries.captured_queries), 6, "one count per source, plus the trip-comment half")

    def test_the_sources_it_counts_are_the_sources_it_renders(self) -> None:
        # A source counted but not rendered (or the reverse) shows a page count
        # that does not match its own list.
        self.assertEqual(set(JOURNAL_SOURCES), set(JOURNAL_SOURCE_COUNTS))

    def test_a_narrowed_feed_counts_only_its_own_sources(self) -> None:
        self.assertEqual(len(JournalFeed(self.profile, sources=["reviews"])), 0)
        self.assertEqual(len(JournalFeed(self.profile, sources=["visits"])), 12)


class JournalPagePaginationTests(TestCase):
    """GET /memories/journal/ renders one page and links to the next."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        for day in range(1, 29):
            pin = _make_pin(self.profile)
            baker.make(PinVisit, pin=pin, visited_at=_aware(2026, 1, day), notes=f"note {day}")

    def test_only_one_page_is_rendered(self) -> None:
        response = self.client.get(reverse("memories.journal"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["journal_entries"]), _JOURNAL_PAGE_SIZE)
        self.assertEqual(response.context["page_obj"].paginator.count, 28)

    def test_the_next_page_holds_the_rest(self) -> None:
        response = self.client.get(reverse("memories.journal"), {"page": 2})

        self.assertEqual(len(response.context["journal_entries"]), 3)

    def test_pagination_is_a_link_not_an_htmx_swap(self) -> None:
        # This is a full-page view; an hx-get would fetch a whole HTML document
        # and swap it into one div.
        response = self.client.get(reverse("memories.journal"))

        self.assertContains(response, 'href="?page=2"')
        self.assertNotContains(response, 'hx-get="?page=2"')
