"""Global search shows a comment only when the thread it sits in would.

Search reads comment bodies into snippets, so every gate the thread applies has to hold here too: the
author's ``comment_visibility``, an author who blocked the viewer, a deactivated author, an upload still
awaiting its scan, and a mention of a location the viewer has not pinned. The single-comment and SQL forms
of the gate are held to the same rules, since search, the by-id endpoints and the paged API each use one.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments import Comment
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.trips.model import TripComment
from urbanlens.dashboard.services.comments.comments import comment_is_visible
from urbanlens.dashboard.services.global_search import GlobalSearchEngine
from urbanlens.dashboard.services.social.friendship import block_profile
from urbanlens.dashboard.services.trips.trip_comments import trip_comment_is_visible

TEXT = "sealed boiler room"


def _comment_results(response) -> list:
    for group in response.groups:
        if group.meta.slug == "comments":
            return group.results
    return []


class _Gates:
    """The cases every comment surface must refuse, run against one host kind."""

    def make_comment(self, **kwargs):
        raise NotImplementedError

    def is_visible(self, comment) -> bool:
        raise NotImplementedError

    def visible_ids(self) -> set[int]:
        raise NotImplementedError

    def assertHidden(self, comment, query: str = "sealed boiler") -> None:
        self.assertEqual(_comment_results(GlobalSearchEngine().search(self.viewer, query, types=["comments"])), [])
        self.assertFalse(self.is_visible(comment))
        self.assertNotIn(comment.pk, self.visible_ids())

    def test_a_visible_comment_is_found(self) -> None:
        comment = self.make_comment()

        self.assertEqual(
            len(_comment_results(GlobalSearchEngine().search(self.viewer, "sealed boiler", types=["comments"]))), 1
        )
        self.assertTrue(self.is_visible(comment))
        self.assertIn(comment.pk, self.visible_ids())

    def test_an_author_who_shows_comments_to_no_one(self) -> None:
        self.author.comment_visibility = VisibilityChoice.NO_ONE
        self.author.save(update_fields=["comment_visibility"])

        self.assertHidden(self.make_comment())

    def test_an_author_who_blocked_the_viewer(self) -> None:
        comment = self.make_comment()
        block_profile(self.author, self.viewer)

        self.assertHidden(comment)

    def test_a_deactivated_author(self) -> None:
        comment = self.make_comment()
        self.author.user.is_active = False
        self.author.user.save(update_fields=["is_active"])

        self.assertHidden(comment)

    def test_an_upload_still_awaiting_its_scan(self) -> None:
        self.assertHidden(self.make_comment(pending_scan=True))

    def test_a_mention_of_a_location_the_viewer_has_not_pinned(self) -> None:
        hidden = baker.make("dashboard.Location")

        self.assertHidden(
            self.make_comment(text=f"{TEXT} near @[Hidden vault](loc:{hidden.uuid})"), query="hidden vault"
        )


class WikiCommentSearchTests(_Gates, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make("auth.User", username="viewer").profile
        self.author = baker.make("auth.User", username="author").profile
        self.author.comment_visibility = VisibilityChoice.ANYONE
        self.author.save(update_fields=["comment_visibility"])
        location = baker.make("dashboard.Location")
        baker.make("dashboard.Pin", profile=self.viewer, location=location)
        baker.make("dashboard.Pin", profile=self.author, location=location)
        self.wiki = baker.make("dashboard.Wiki", location=location)

    def make_comment(self, **kwargs) -> Comment:
        return baker.make(Comment, pin=None, wiki=self.wiki, profile=self.author, **{"text": TEXT, **kwargs})

    def is_visible(self, comment: Comment) -> bool:
        return comment_is_visible(Comment.objects.get(pk=comment.pk), self.viewer)

    def visible_ids(self) -> set[int]:
        return set(Comment.objects.visible_to(self.viewer).values_list("pk", flat=True))


class TripCommentSearchTests(_Gates, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.viewer = baker.make("auth.User", username="trip-viewer").profile
        self.author = baker.make("auth.User", username="trip-author").profile
        self.author.comment_visibility = VisibilityChoice.ANYONE
        self.author.save(update_fields=["comment_visibility"])
        self.trip = baker.make("dashboard.Trip", creator=self.viewer, name="Factory weekend")
        baker.make("dashboard.TripMembership", trip=self.trip, profile=self.viewer)
        baker.make("dashboard.TripMembership", trip=self.trip, profile=self.author)

    def make_comment(self, **kwargs) -> TripComment:
        return baker.make(TripComment, trip=self.trip, author=self.author, **{"text": TEXT, **kwargs})

    def is_visible(self, comment: TripComment) -> bool:
        return trip_comment_is_visible(TripComment.objects.get(pk=comment.pk), self.viewer)

    def visible_ids(self) -> set[int]:
        return set(TripComment.objects.visible_to(self.viewer).values_list("pk", flat=True))
