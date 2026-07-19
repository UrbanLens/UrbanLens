"""Tests for the pin-comments panel's "Notes" wording (UL prompt: rename the
Comments subnav to Notes on the pin detail page).

A pin's comments are private to its owner - there's no one else to converse
with - so "Comments" implied a shared discussion that doesn't exist there.
Wiki (and trip) comments are genuinely shared between users and must keep the
"Comments" label; both render through the same comment_panel.html/Comment
model, distinguished only by ``context_type``, so these tests also guard
against the pin-only wording leaking into the shared-comments contexts.
"""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase


def _location_with_wiki(name: str = "Old Mill"):
    location = baker.make("dashboard.Location")
    wiki = baker.make("dashboard.Wiki", location=location, name=name)
    return location, wiki


class PinNotesLabelingTests(TestCase):
    """GET /map/pin/<slug>/comments/ - pin comments, labeled "Notes"."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location = baker.make("dashboard.Location", latitude="41.0", longitude="-73.0")
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def _url(self):
        return reverse("pin.comments", args=[self.pin.slug])

    def test_panel_header_says_notes_not_comments(self):
        response = self.client.get(self._url())

        content = response.content.decode()
        self.assertIn(">Notes<", content)
        self.assertIn("sticky_note_2", content)
        self.assertNotIn(">Comments<", content)

    def test_pin_detail_subnav_tab_says_notes(self):
        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))

        content = response.content.decode()
        self.assertIn('data-tab="comments"', content)
        self.assertIn(">Notes</span>", content)

    def test_empty_state_explains_notes_are_private(self):
        response = self.client.get(self._url())

        content = response.content.decode()
        self.assertIn("No notes yet", content)
        self.assertIn("comment-panel-empty", content)
        self.assertIn("private to you", content)

    def test_empty_state_is_absent_once_a_note_exists(self):
        baker.make("dashboard.Comment", profile=self.profile, pin=self.pin, parent=None, text="Watch the third floor.")

        response = self.client.get(self._url())

        self.assertNotIn("comment-panel-empty", response.content.decode())

    def test_compose_placeholder_says_note(self):
        response = self.client.get(self._url())

        self.assertIn("Add a note...", response.content.decode())


class WikiCommentsKeepTheCommentsLabelTests(TestCase):
    """Wiki comments are genuinely shared between users - regression guard
    against the pin-only "Notes" rename leaking into this shared context."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location, self.wiki = _location_with_wiki()
        baker.make("dashboard.Pin", profile=self.profile, location=self.location)

    def _url(self):
        return reverse("location.wiki.comments", args=[self.location.slug])

    def test_panel_header_still_says_comments(self):
        response = self.client.get(self._url())

        content = response.content.decode()
        self.assertIn(">Comments<", content)
        self.assertNotIn(">Notes<", content)

    def test_empty_state_uses_comments_wording(self):
        response = self.client.get(self._url())

        content = response.content.decode()
        self.assertIn("No comments yet", content)
        self.assertIn("comment-panel-empty", content)

    def test_compose_placeholder_still_says_comment(self):
        response = self.client.get(self._url())

        self.assertIn("Add a comment...", response.content.decode())
