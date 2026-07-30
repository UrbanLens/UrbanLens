"""Tests for aggregating a pin's notes (private Comments) across its child pins.

Mirrors the page-wide "show child pin details" toggle already applied to the
map, photo gallery, and visit history: a note left on a child pin must not be
invisible from the parent's own Notes tab just because it lives on a nested
row. Posting and deleting still always act on the exact pin/comment in the
URL - only the listing (``?children=1``) aggregates.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin

_coord_counter = 0


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 47.0 + _coord_counter * 0.001)
    kwargs.setdefault("longitude", -90.0 - _coord_counter * 0.001)
    return baker.make(Location, google_place=None, **kwargs)


class PinCommentsAggregationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.parent = baker.make(Pin, profile=self.profile, location=_make_location(), slug="campus")
        self.child = baker.make(Pin, profile=self.profile, parent_pin=self.parent, location=_make_location(), name="Tool Shed")
        self.parent_note = baker.make(Comment, pin=self.parent, wiki=None, profile=self.profile, text="a note on the parent")
        self.child_note = baker.make(Comment, pin=self.child, wiki=None, profile=self.profile, text="a note on the child")

    def test_default_view_shows_only_the_parents_own_notes(self) -> None:
        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.parent.slug}))
        self.assertContains(response, "a note on the parent")
        self.assertNotContains(response, "a note on the child")

    def test_children_flag_aggregates_notes_from_sub_pins(self) -> None:
        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.parent.slug}), {"children": "1"})
        self.assertContains(response, "a note on the parent")
        self.assertContains(response, "a note on the child")

    def test_an_aggregated_child_note_is_labelled_with_its_sub_pin(self) -> None:
        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.parent.slug}), {"children": "1"})
        self.assertContains(response, "Tool Shed")
        self.assertContains(response, reverse("pin.details", kwargs={"pin_slug": self.child.slug}))

    def test_the_parents_own_note_is_never_labelled(self) -> None:
        """Only aggregated child notes get the 'written on the child pin' chip - one chip
        total, for the one child note, never for the parent's own."""
        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.parent.slug}), {"children": "1"})
        self.assertEqual(response.content.decode().count("comment-child-chip"), 1)

    def test_grandchildren_notes_are_included_at_any_depth(self) -> None:
        grandchild = baker.make(Pin, profile=self.profile, parent_pin=self.child, location=_make_location())
        baker.make(Comment, pin=grandchild, wiki=None, profile=self.profile, text="a note two levels down")
        response = self.client.get(reverse("pin.comments", kwargs={"pin_slug": self.parent.slug}), {"children": "1"})
        self.assertContains(response, "a note two levels down")

    def test_posting_always_creates_on_the_exact_pin_in_the_url(self) -> None:
        response = self.client.post(
            reverse("pin.comments", kwargs={"pin_slug": self.parent.slug}) + "?children=1",
            {"text": "posted while aggregated"},
        )
        self.assertEqual(response.status_code, 200)
        new_comment = Comment.objects.get(text="posted while aggregated")
        self.assertEqual(new_comment.pin_id, self.parent.pk)

    def test_deleting_a_child_notes_comment_via_the_parent_url_works(self) -> None:
        """The delete button always posts back to the parent pin's URL (see
        _comment_body.html), even for a note that lives on a child pin."""
        response = self.client.delete(reverse("pin.comment.delete", kwargs={"pin_slug": self.parent.slug, "comment_id": self.child_note.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Comment.objects.filter(pk=self.child_note.pk).exists())

    def test_cannot_delete_a_note_outside_the_pins_own_subtree(self) -> None:
        unrelated = baker.make(Pin, profile=self.profile, location=_make_location())
        unrelated_note = baker.make(Comment, pin=unrelated, wiki=None, profile=self.profile, text="not part of this hierarchy")
        response = self.client.delete(reverse("pin.comment.delete", kwargs={"pin_slug": self.parent.slug, "comment_id": unrelated_note.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Comment.objects.filter(pk=unrelated_note.pk).exists())

    def test_deleting_someone_elses_note_within_the_subtree_is_forbidden(self) -> None:
        other_profile = baker.make(User).profile
        others_note = baker.make(Comment, pin=self.child, wiki=None, profile=other_profile, text="someone else's note")
        response = self.client.delete(reverse("pin.comment.delete", kwargs={"pin_slug": self.parent.slug, "comment_id": others_note.pk}))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Comment.objects.filter(pk=others_note.pk).exists())
