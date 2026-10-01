"""One page of comments must cost one page, however long the thread is.

The external comment endpoints built the *whole* tree - every top-level
comment, every reply, every reaction, every markup map, and a visibility check
per author - and then paginated the built list. The page-size parameter bounded
the response body and nothing else, so a wiki that accumulated comments got
slower for everyone reading it, one page at a time.

It was answered that way because the three visibility gates lived in Python:
which rows a page should contain was not knowable in SQL. They are a queryset
now (``Comment.objects.visible_to``, held to the per-row check by
``test_comment_visibility_is_a_queryset``), so the database can do the cut.

Measured as objects instantiated and rows read rather than wall time, because
both are exact and neither moves when the host is busy.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.db import connection
from model_bakery import baker

from urbanlens.core.tests.endpoint_scaling import _row_counting_wrapper
from urbanlens.core.tests.instantiation_scaling import count_instantiations
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.tests.hypothesis.test_external_api_wiki_oracle import grant_wiki_scopes

BASE = "/dashboard/api/external/v1/wikis"

#: Comfortably above ExternalApiPagination.page_size (25), so both measurements
#: below render a *full* page and the only thing differing is the size of the
#: thread behind it.
FIRST_BATCH = 30
SECOND_BATCH = 120


class ExternalCommentListPagingTests(TestCase):
    """Reading one page must not read the whole thread."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _key, self.raw_key = generate_api_key(self.user, "Comment client")
        grant_wiki_scopes(self.user)

        self.location = baker.make("dashboard.Location")
        self.wiki = baker.make("dashboard.Wiki", location=self.location, name="Old Mill")
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

        self.author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()

    def _seed(self, count: int) -> None:
        for index in range(count):
            Comment.objects.create(wiki=self.wiki, profile=self.author, text=f"comment {index}")

    def _get(self):
        return self.client.get(
            f"{BASE}/{self.location.ensure_slug()}/comments/", HTTP_AUTHORIZATION=f"Bearer {self.raw_key}"
        )

    def _cost(self) -> tuple[int, int]:
        """Objects instantiated and rows read serving one page."""
        rows = [0]
        with connection.execute_wrapper(_row_counting_wrapper(rows)), count_instantiations() as counted:
            response = self._get()
        self.assertEqual(response.status_code, 200)
        return counted.total, rows[0]

    def test_a_page_costs_the_same_however_long_the_thread_is(self) -> None:
        self._seed(FIRST_BATCH)
        baseline_objects, baseline_rows = self._cost()

        self._seed(SECOND_BATCH)
        after_objects, after_rows = self._cost()

        self.assertLessEqual(
            after_objects,
            baseline_objects,
            f"Adding {SECOND_BATCH} comments nobody asked for took one page from {baseline_objects} objects to {after_objects}.",
        )
        self.assertLessEqual(
            after_rows,
            baseline_rows,
            f"Adding {SECOND_BATCH} comments nobody asked for took one page from {baseline_rows} rows read to {after_rows}.",
        )

    def test_the_page_is_still_a_full_page_and_the_count_is_the_visible_total(self) -> None:
        """A gate answered in SQL means the page is exact, not merely cheaper."""
        self._seed(FIRST_BATCH)
        body = self._get().json()
        self.assertEqual(len(body["results"]), 25)
        self.assertEqual(body["count"], FIRST_BATCH)

    def test_a_thread_the_viewer_may_only_partly_see_still_fills_its_pages(self) -> None:
        """The count and the page agree even when a gate hides most of the thread."""
        self._seed(FIRST_BATCH)
        hidden_author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=hidden_author.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        for index in range(50):
            Comment.objects.create(wiki=self.wiki, profile=hidden_author, text=f"hidden {index}")

        body = self._get().json()
        self.assertEqual(body["count"], FIRST_BATCH)
        self.assertEqual(len(body["results"]), 25)
        self.assertTrue(all(row["text"].startswith("comment ") for row in body["results"]))
