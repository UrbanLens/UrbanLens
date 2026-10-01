"""A page of the comments panel holds a page's worth of comments.

The panel paged its queryset in SQL and *then* dropped rows in Python, because
the visibility gates were only answerable there. A viewer who could not see
most of a thread got a page of two where the page size is eight, and paging
back gave them more short pages rather than the rest of what they could read.

That is the same defect as H47's on the external API, just with the symptom
showing in the page rather than in the cost: a filter applied after the cut
makes the cut mean nothing. ``Comment.objects.visible_to`` moves it before.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.comments import _COMMENTS_PAGE_SIZE, _build_context
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile


class CommentPanelPagingTests(TestCase):
    """Hidden comments must not eat into the page the viewer asked for."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to site admin
        self.viewer = Profile.objects.get(user=baker.make(User))
        self.pin = baker.make(Pin, profile=self.viewer, location=baker.make(Location))

        self.readable = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.readable.pk).update(comment_visibility=VisibilityChoice.ANYONE)
        self.hidden = Profile.objects.get(user=baker.make(User))
        Profile.objects.filter(pk=self.hidden.pk).update(comment_visibility=VisibilityChoice.NO_ONE)

    def _context(self) -> dict:
        request = RequestFactory().get("/")
        return _build_context(self.pin.comments.all(), self.viewer, request, pin=self.pin, context_type="pin")

    def test_a_page_is_full_even_when_most_of_the_thread_is_hidden(self) -> None:
        # Interleaved, so any page of the raw queryset carries both kinds.
        for index in range(_COMMENTS_PAGE_SIZE * 4):
            Comment.objects.create(pin=self.pin, profile=self.readable, text=f"readable {index}")
            Comment.objects.create(pin=self.pin, profile=self.hidden, text=f"hidden {index}")

        rendered = self._context()["rendered_comments"]

        self.assertEqual(
            len(rendered), _COMMENTS_PAGE_SIZE, f"the page rendered {len(rendered)} of {_COMMENTS_PAGE_SIZE}"
        )
        self.assertTrue(all(item["comment"].profile_id == self.readable.pk for item in rendered))

    def test_the_pager_counts_pages_of_visible_comments(self) -> None:
        """A page count derived from rows the viewer cannot see offers empty pages."""
        for index in range(_COMMENTS_PAGE_SIZE):
            Comment.objects.create(pin=self.pin, profile=self.readable, text=f"readable {index}")
        for index in range(_COMMENTS_PAGE_SIZE * 5):
            Comment.objects.create(pin=self.pin, profile=self.hidden, text=f"hidden {index}")

        page_obj = self._context()["page_obj"]

        self.assertEqual(page_obj.paginator.num_pages, 1)
        self.assertEqual(page_obj.paginator.count, _COMMENTS_PAGE_SIZE)

    def test_a_thread_the_viewer_can_read_entirely_is_unchanged(self) -> None:
        for index in range(_COMMENTS_PAGE_SIZE - 2):
            Comment.objects.create(pin=self.pin, profile=self.readable, text=f"readable {index}")

        self.assertEqual(len(self._context()["rendered_comments"]), _COMMENTS_PAGE_SIZE - 2)
