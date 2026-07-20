"""Tests for the max-length limits added to previously-unbounded free-text fields
(``services/text_limits.py``): Pin.description, Wiki.description, Comment.text,
Trip.description, TripActivity.notes, TripComment.text, PinMarkup.label, Profile.bio.

Two layers are verified:
  - Model-level: ``full_clean()`` raises ``ValidationError`` for text longer than
    the field's ``max_length`` (these are ``TextField``s, so Postgres itself
    enforces nothing - the limit only exists via Django's validators).
  - Controller-level: the write paths that build/mutate these models directly
    (bypassing a Form/Serializer's automatic ``full_clean()``) explicitly check
    length via ``text_length_error()`` and return 400 rather than silently
    persisting oversized input.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_edit import PinEditView
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.markup.model import PinMarkup
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.trips.model import Trip, TripActivity, TripComment, TripMembership
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.text_limits import (
    MAX_COMMENT_TEXT_LENGTH,
    MAX_MARKUP_LABEL_LENGTH,
    MAX_PIN_DESCRIPTION_LENGTH,
    MAX_PROFILE_BIO_LENGTH,
    MAX_TRIP_ACTIVITY_NOTES_LENGTH,
    MAX_TRIP_DESCRIPTION_LENGTH,
    MAX_WIKI_DESCRIPTION_LENGTH,
)

if TYPE_CHECKING:
    from django.http import HttpResponseBase

    from urbanlens.dashboard.models.profile.model import Profile


def _make_trip(creator_profile: Profile, **kwargs) -> Trip:
    trip = Trip.objects.create(name=kwargs.pop("name", "Test Trip"), creator=creator_profile, **kwargs)
    TripMembership.objects.get_or_create(trip=trip, profile=creator_profile, defaults={"rsvp": "yes"})
    return trip


def _location_with_wiki(name: str = "Old Mill") -> tuple[Location, Wiki]:
    location = baker.make(Location)
    wiki = baker.make(Wiki, location=location, name=name)
    return location, wiki


class ModelFullCleanLengthTests(TestCase):
    """`full_clean()` must reject text past each field's `max_length`.

    Each case starts from a fully-valid, already-saved instance (via
    ``baker.make``, so every other required field - FKs included - is
    already populated) and mutates only the field under test, then asserts
    the raised ``ValidationError`` names *that* field specifically. Plain
    Django ``TextField``s do **not** get this for free from `max_length=N`
    alone (unlike ``CharField``) - `full_clean()` only enforces it because
    the model fields also carry an explicit `validators=[MaxLengthValidator(N)]`.
    """

    def test_pin_description_too_long(self) -> None:
        pin = baker.make(Pin)
        pin.description = "x" * (MAX_PIN_DESCRIPTION_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            pin.full_clean()
        self.assertIn("description", cm.exception.message_dict)

    def test_pin_description_at_limit_is_valid(self) -> None:
        pin = baker.make(Pin)
        pin.description = "x" * MAX_PIN_DESCRIPTION_LENGTH
        pin.full_clean()

    def test_wiki_description_too_long(self) -> None:
        wiki = baker.make(Wiki)
        wiki.description = "x" * (MAX_WIKI_DESCRIPTION_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            wiki.full_clean()
        self.assertIn("description", cm.exception.message_dict)

    def test_comment_text_too_long(self) -> None:
        pin = baker.make(Pin)
        comment = baker.make(Comment, pin=pin, wiki=None)
        comment.text = "x" * (MAX_COMMENT_TEXT_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            comment.full_clean()
        self.assertIn("text", cm.exception.message_dict)

    def test_trip_description_too_long(self) -> None:
        trip = baker.make(Trip)
        trip.description = "x" * (MAX_TRIP_DESCRIPTION_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            trip.full_clean()
        self.assertIn("description", cm.exception.message_dict)

    def test_trip_activity_notes_too_long(self) -> None:
        activity = baker.make(TripActivity)
        activity.notes = "x" * (MAX_TRIP_ACTIVITY_NOTES_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            activity.full_clean()
        self.assertIn("notes", cm.exception.message_dict)

    def test_trip_comment_text_too_long(self) -> None:
        comment = baker.make(TripComment)
        comment.text = "x" * (MAX_COMMENT_TEXT_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            comment.full_clean()
        self.assertIn("text", cm.exception.message_dict)

    def test_pin_markup_label_too_long(self) -> None:
        markup = baker.make(PinMarkup, markup_type="line", geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
        markup.label = "x" * (MAX_MARKUP_LABEL_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            markup.full_clean()
        self.assertIn("label", cm.exception.message_dict)

    def test_profile_bio_too_long(self) -> None:
        profile = baker.make(User).profile
        profile.bio = "x" * (MAX_PROFILE_BIO_LENGTH + 1)
        with self.assertRaises(ValidationError) as cm:
            profile.full_clean()
        self.assertIn("bio", cm.exception.message_dict)


class PinEditDescriptionLengthTests(TestCase):
    """POST /map/pin/<slug>/edit/ - PinEditView must reject an oversized description."""

    def setUp(self) -> None:
        super().setUp()
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)

    def _post(self, body: dict) -> HttpResponseBase:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def test_oversized_description_rejected(self) -> None:
        response = self._post({"description": "x" * (MAX_PIN_DESCRIPTION_LENGTH + 1)})
        self.assertEqual(response.status_code, 400)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.description)

    def test_description_at_limit_accepted(self) -> None:
        text = "x" * MAX_PIN_DESCRIPTION_LENGTH
        response = self._post({"description": text})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.description, text)


class PinBulkEditDescriptionLengthTests(TestCase):
    """POST /map/pins/bulk-edit/ - PinBulkEditView must reject an oversized description."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def test_oversized_description_rejected(self) -> None:
        resp = self.client.post(
            reverse("pin.bulk_edit"),
            data=json.dumps({"uuids": [str(self.pin.uuid)], "description": "x" * (MAX_PIN_DESCRIPTION_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.description)


class WikiEditDescriptionLengthTests(TestCase):
    """POST /location/<slug>/wiki/edit/ - must reject an oversized description."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)
        self.location, self.wiki = _location_with_wiki()
        baker.make(Pin, profile=self.user.profile, location=self.location)

    def test_oversized_description_rejected(self) -> None:
        resp = self.client.post(
            reverse("location.wiki.edit", args=[self.location.slug]),
            data=json.dumps({"description": "x" * (MAX_WIKI_DESCRIPTION_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.wiki.refresh_from_db()
        self.assertNotEqual(len(self.wiki.description or ""), MAX_WIKI_DESCRIPTION_LENGTH + 1)

    def test_description_within_limit_accepted(self) -> None:
        text = "y" * 100
        resp = self.client.post(
            reverse("location.wiki.edit", args=[self.location.slug]),
            data=json.dumps({"description": text}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        self.wiki.refresh_from_db()
        self.assertEqual(self.wiki.description, text)


class PinCommentTextLengthTests(TestCase):
    """POST /map/pin/<slug>/comments/ - must reject oversized comment text."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def test_oversized_text_rejected(self) -> None:
        resp = self.client.post(
            reverse("pin.comments", args=[self.pin.slug]),
            {"text": "x" * (MAX_COMMENT_TEXT_LENGTH + 1)},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Comment.objects.filter(pin=self.pin).exists())


class WikiCommentTextLengthTests(TestCase):
    """POST /location/<slug>/wiki/comments/ - must reject oversized comment text."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client = Client()
        self.client.force_login(self.user)
        self.profile = self.user.profile
        self.location, self.wiki = _location_with_wiki()
        baker.make(Pin, profile=self.profile, location=self.location)

    def test_oversized_text_rejected(self) -> None:
        resp = self.client.post(
            reverse("location.wiki.comments", args=[self.location.slug]),
            {"text": "x" * (MAX_COMMENT_TEXT_LENGTH + 1)},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(self.wiki.comments.exists())


class TripDescriptionLengthTests(TestCase):
    """trips.create / trips.edit - must reject an oversized description."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)

    def test_create_rejects_oversized_description(self) -> None:
        resp = self.client.post(
            reverse("trips.create"),
            data=json.dumps({"name": "Trip", "description": "x" * (MAX_TRIP_DESCRIPTION_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Trip.objects.filter(name="Trip").exists())

    def test_edit_rejects_oversized_description(self) -> None:
        trip = _make_trip(self.profile)
        resp = self.client.post(
            reverse("trips.edit", kwargs={"trip_slug": trip.slug}),
            data=json.dumps({"description": "x" * (MAX_TRIP_DESCRIPTION_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        trip.refresh_from_db()
        self.assertIsNone(trip.description)


class TripActivityNotesLengthTests(TestCase):
    """trips.activities (POST=create) / trips.activity.edit - must reject oversized notes."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.trip = _make_trip(self.profile, allow_add_activities=Trip.PERM_EVERYONE, allow_edit_activities=Trip.PERM_EVERYONE)

    def test_create_rejects_oversized_notes(self) -> None:
        resp = self.client.post(
            reverse("trips.activities", kwargs={"trip_slug": self.trip.slug}),
            data=json.dumps({"title": "Stop", "notes": "x" * (MAX_TRIP_ACTIVITY_NOTES_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TripActivity.objects.filter(trip=self.trip, title="Stop").exists())

    def test_edit_rejects_oversized_notes(self) -> None:
        activity = baker.make(TripActivity, trip=self.trip, notes="short")
        resp = self.client.post(
            reverse("trips.activity.edit", kwargs={"trip_slug": self.trip.slug, "activity_id": activity.id}),
            data=json.dumps({"notes": "x" * (MAX_TRIP_ACTIVITY_NOTES_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        activity.refresh_from_db()
        self.assertEqual(activity.notes, "short")


class TripCommentTextLengthTests(TestCase):
    """POST /trips/<uuid>/comments/ - must reject oversized comment text."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.trip = _make_trip(self.profile, allow_comments=Trip.PERM_EVERYONE)

    def test_oversized_text_rejected(self) -> None:
        resp = self.client.post(
            reverse("trips.comments", kwargs={"trip_slug": self.trip.slug}),
            {"text": "x" * (MAX_COMMENT_TEXT_LENGTH + 1)},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(TripComment.objects.filter(trip=self.trip).exists())


class MarkupLabelLengthTests(TestCase):
    """pin.markup (POST=create) / pin.markup.edit - must reject an oversized label."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client = Client()
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def test_create_rejects_oversized_label(self) -> None:
        resp = self.client.post(
            reverse("pin.markup", args=[self.pin.slug]),
            data=json.dumps({
                "markup_type": "line",
                "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
                "label": "x" * (MAX_MARKUP_LABEL_LENGTH + 1),
            }),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PinMarkup.objects.filter(parent_pin=self.pin).exists())

    def test_edit_rejects_oversized_label(self) -> None:
        markup = baker.make(
            PinMarkup,
            parent_pin=self.pin,
            profile=self.profile,
            markup_type="line",
            geometry={"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            label="short",
        )
        resp = self.client.post(
            reverse("pin.markup.edit", args=[self.pin.slug, markup.uuid]),
            data=json.dumps({"label": "x" * (MAX_MARKUP_LABEL_LENGTH + 1)}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        markup.refresh_from_db()
        self.assertEqual(markup.label, "short")
