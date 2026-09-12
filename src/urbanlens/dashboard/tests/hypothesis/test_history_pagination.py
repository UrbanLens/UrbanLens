"""Both revision-history lists render one page at a time (P69)."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.article import _HISTORY_PAGE_SIZE as ARTICLE_PAGE_SIZE
from urbanlens.dashboard.controllers.location_wiki import _HISTORY_PAGE_SIZE as WIKI_PAGE_SIZE
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki_edit.model import WikiEdit
from urbanlens.dashboard.services.wiki.articles import save_article


class ArticleHistoryPaginationTests(TestCase):
    """GET .../article/history/ - one page of revisions, numbered absolutely."""

    def setUp(self) -> None:
        baker.make("auth.User")  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def _history(self, page: int | None = None):
        url = reverse("pin.article.history", args=[self.pin.slug])
        return self.client.get(url if page is None else f"{url}?page={page}")

    def _write(self, count: int) -> None:
        for index in range(count):
            # Each revision one character longer than the last, so a delta
            # computed against the right predecessor is always exactly +1.
            save_article(editor=self.profile, content="x" * (index + 1), pin=self.pin)

    def test_first_page_holds_only_one_page_of_rows(self) -> None:
        self._write(ARTICLE_PAGE_SIZE + 5)

        response = self._history()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count('class="article-history-row'), ARTICLE_PAGE_SIZE)

    def test_numbering_continues_onto_the_second_page(self) -> None:
        total = ARTICLE_PAGE_SIZE + 5
        self._write(total)

        first = self._history().content.decode()
        second = self._history(page=2).content.decode()

        self.assertIn(f"#{total}", first, "the newest revision keeps the highest number")
        self.assertNotIn("#1<", first, "the oldest revision is not on the first page")
        self.assertIn("#1<", second, "the second page ends at revision #1")
        self.assertNotIn(f"#{total}<", second)

    def test_only_the_newest_revision_is_marked_current(self) -> None:
        self._write(ARTICLE_PAGE_SIZE + 5)

        first = self._history().content.decode()
        second = self._history(page=2).content.decode()

        self.assertEqual(first.count("article-history-chip--current"), 1)
        self.assertEqual(second.count("article-history-chip--current"), 0, "page two has no current revision")

    def test_a_later_page_can_still_restore_its_top_row(self) -> None:
        self._write(ARTICLE_PAGE_SIZE + 5)

        second = self._history(page=2).content.decode()

        # The Restore button is hidden for the current revision only. Keying
        # that off "first row on this page" takes it away from a row the user
        # is entitled to restore.
        self.assertEqual(second.count("Restore this version"), 5)

    def test_the_oldest_row_on_a_page_is_sized_against_the_next_page(self) -> None:
        self._write(ARTICLE_PAGE_SIZE + 5)

        first = self._history().content.decode()

        # Every revision here is one character longer than its predecessor, so
        # the only rows that may read anything but "+1" are the very first
        # revision (against nothing) - which is not on this page at all.
        self.assertEqual(first.count("+1\n"), ARTICLE_PAGE_SIZE, first[:400])

    def test_pagination_links_target_the_history_url(self) -> None:
        self._write(ARTICLE_PAGE_SIZE + 5)

        response = self._history()

        self.assertContains(response, f"{reverse('pin.article.history', args=[self.pin.slug])}?page=2")


class WikiHistoryPaginationTests(TestCase):
    """GET /location/<slug>/wiki/history/ - one page of field edits."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make(Pin, profile=self.profile, location=self.location)
        self.client.force_login(self.user)

    def _edits(self, count: int) -> list[WikiEdit]:
        return [
            baker.make(
                "dashboard.WikiEdit",
                wiki=self.wiki,
                editor=self.profile,
                changes={"name": {"from": f"name {index}", "to": f"name {index + 1}"}},
                reverted=False,
            )
            for index in range(count)
        ]

    def _history(self, page: int | None = None):
        url = reverse("location.wiki.history", args=[self.location.slug])
        return self.client.get(url if page is None else f"{url}?page={page}")

    def test_first_page_holds_only_one_page_of_rows(self) -> None:
        self._edits(WIKI_PAGE_SIZE + 4)

        response = self._history()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode().count('class="wiki-history-item'), WIKI_PAGE_SIZE)

    def test_the_remainder_is_on_the_second_page(self) -> None:
        self._edits(WIKI_PAGE_SIZE + 4)

        response = self._history(page=2)

        self.assertEqual(response.content.decode().count('class="wiki-history-item'), 4)

    def test_pagination_links_target_the_history_url_not_the_current_path(self) -> None:
        # _render_history is shared with the revert and delete actions, which
        # POST to their own URLs. A pagination link built from request.path
        # would swap a revert into the list on the next click.
        self._edits(WIKI_PAGE_SIZE + 4)
        edits = list(WikiEdit.objects.filter(wiki=self.wiki).order_by("-created"))

        response = self.client.post(
            reverse("location.wiki.history.delete", args=[self.location.slug, edits[0].pk]),
            {"page": "1"},
        )

        self.assertEqual(response.status_code, 200)
        history_url = reverse("location.wiki.history", args=[self.location.slug])
        self.assertContains(response, f"{history_url}?page=2")
        self.assertNotContains(response, "/delete/?page=")

    def test_an_action_keeps_the_user_on_their_page(self) -> None:
        self._edits(WIKI_PAGE_SIZE + 4)
        page_two = list(WikiEdit.objects.filter(wiki=self.wiki).order_by("-created"))[WIKI_PAGE_SIZE:]

        response = self.client.post(
            reverse("location.wiki.history.delete", args=[self.location.slug, page_two[0].pk]),
            {"page": "2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f"Page 2 of {2}")
