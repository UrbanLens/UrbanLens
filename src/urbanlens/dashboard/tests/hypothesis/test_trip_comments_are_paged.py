"""A trip thread must cost a page, not a thread.

H43 and H52: ``build_comment_tree`` loads every top-level comment and every
reply on a trip, renders each one's text, aggregates each one's reactions, and
resolves each author's identity - and then the panel renders all of it while
the API paginates the built list. page_size bounded the response body and
nothing else, and the panel bounded nothing at all, so a trip everybody talks
on gets slower for everybody on it.

Posting was the sharpest case: serializing the one comment just created went
back through the whole tree to find it.

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
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip, TripComment, TripMembership
from urbanlens.dashboard.services.auth.api_keys import generate_api_key

#: Comfortably above the external API's page size (25) in both measurements, so
#: the only thing differing is the size of the thread behind a full page.
FIRST_BATCH = 30
SECOND_BATCH = 120


class _TripThreadCase(TestCase):
    """A trip the viewer is on, and an author they are allowed to read."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.trip = baker.make(Trip, creator=self.profile, name="Weekend trip")
        TripMembership.objects.create(trip=self.trip, profile=self.profile)

        self.author = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.author.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.author.refresh_from_db()
        TripMembership.objects.create(trip=self.trip, profile=self.author)

    def _seed(self, count: int) -> None:
        for index in range(count):
            TripComment.objects.create(trip=self.trip, author=self.author, text=f"comment {index}")


class TripCommentApiPagingTests(_TripThreadCase):
    """Reading one page of the API must not read the whole thread."""

    def setUp(self) -> None:
        super().setUp()
        key, self.raw_key = generate_api_key(self.user, "Trip client")
        ApiKey.objects.filter(pk=key.pk).update(
            scopes=[ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value, ApiKeyScope.PROFILE_READ.value]
        )

    def _get(self):
        return self.client.get(
            f"/dashboard/api/external/v1/trips/{self.trip.slug}/comments/", HTTP_AUTHORIZATION=f"Bearer {self.raw_key}"
        )

    def _cost(self) -> tuple[int, int]:
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
            f"Adding {SECOND_BATCH} comments took one page from {baseline_objects} objects to {after_objects}.",
        )
        self.assertLessEqual(
            after_rows,
            baseline_rows,
            f"Adding {SECOND_BATCH} comments took one page from {baseline_rows} rows read to {after_rows}.",
        )

    def test_the_page_is_full_and_the_count_is_the_visible_total(self) -> None:
        self._seed(FIRST_BATCH)
        body = self._get().json()
        self.assertEqual(len(body["results"]), 25)
        self.assertEqual(body["count"], FIRST_BATCH)

    def test_a_thread_the_viewer_may_only_partly_see_still_fills_its_pages(self) -> None:
        self._seed(FIRST_BATCH)
        hidden = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=hidden.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=self.trip, profile=hidden)
        for index in range(50):
            TripComment.objects.create(trip=self.trip, author=hidden, text=f"hidden {index}")

        body = self._get().json()
        self.assertEqual(body["count"], FIRST_BATCH)
        self.assertEqual(len(body["results"]), 25)
        self.assertTrue(all(row["text"].startswith("comment ") for row in body["results"]))


class PostingDoesNotBuildTheThreadTests(_TripThreadCase):
    """Serializing the comment just created must not re-read everything else."""

    def _cost_of_posting(self) -> int:
        self.client.force_login(self.user)
        rows = [0]
        with connection.execute_wrapper(_row_counting_wrapper(rows)):
            response = self.client.post(f"/dashboard/trips/{self.trip.slug}/comments/", {"text": "and another"})
        self.assertIn(response.status_code, (200, 302))
        return rows[0]

    def test_posting_costs_the_same_however_long_the_thread_is(self) -> None:
        self._seed(FIRST_BATCH)
        baseline = self._cost_of_posting()

        self._seed(SECOND_BATCH)
        after = self._cost_of_posting()

        self.assertLessEqual(
            after, baseline, f"Adding {SECOND_BATCH} comments took posting one from {baseline} rows read to {after}."
        )


class TripCommentPanelPagingTests(_TripThreadCase):
    """The panel must render a page, and say there are more."""

    def _panel(self, page: int | None = None):
        self.client.force_login(self.user)
        query = f"?page={page}" if page is not None else ""
        return self.client.get(f"/dashboard/trips/{self.trip.slug}/comments/{query}")

    def test_the_panel_renders_a_bounded_page_of_a_long_thread(self) -> None:
        self._seed(FIRST_BATCH)
        response = self._panel()
        self.assertEqual(response.status_code, 200)
        page_obj = response.context["page_obj"]
        self.assertLess(page_obj.paginator.per_page, FIRST_BATCH)
        self.assertGreater(page_obj.paginator.num_pages, 1)
        self.assertLessEqual(len(response.context["rendered_comments"]), page_obj.paginator.per_page)

    def test_a_full_page_is_full(self) -> None:
        """A short page must mean arithmetic, not rows the gate dropped after the cut."""
        self._seed(FIRST_BATCH)
        response = self._panel(page=1)
        page_obj = response.context["page_obj"]
        self.assertEqual(len(response.context["rendered_comments"]), page_obj.paginator.per_page)

    def test_a_full_page_is_full_even_when_most_of_the_thread_is_hidden(self) -> None:
        hidden = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=hidden.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=self.trip, profile=hidden)
        # Interleaved, so any page of the raw thread would carry both kinds.
        for index in range(FIRST_BATCH):
            TripComment.objects.create(trip=self.trip, author=self.author, text=f"comment {index}")
            TripComment.objects.create(trip=self.trip, author=hidden, text=f"hidden {index}")

        response = self._panel(page=1)
        page_obj = response.context["page_obj"]
        self.assertEqual(len(response.context["rendered_comments"]), page_obj.paginator.per_page)

    def test_the_badge_counts_the_whole_visible_thread_not_the_page(self) -> None:
        """A count taken from the page would shrink the moment paging started."""
        self._seed(FIRST_BATCH)
        self.assertEqual(response_count(self._panel()), FIRST_BATCH)

    def test_hidden_comments_reach_neither_the_page_nor_the_badge(self) -> None:
        self._seed(FIRST_BATCH)
        hidden = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=hidden.pk).update(comment_visibility=VisibilityChoice.NO_ONE)
        TripMembership.objects.create(trip=self.trip, profile=hidden)
        for index in range(50):
            TripComment.objects.create(trip=self.trip, author=hidden, text=f"hidden {index}")

        response = self._panel()
        self.assertEqual(response_count(response), FIRST_BATCH)
        self.assertTrue(
            all(item["comment"].author_id == self.author.pk for item in response.context["rendered_comments"])
        )


def response_count(response) -> int:
    """The comment-count badge the panel rendered."""
    return int(response.context["comment_count"])
