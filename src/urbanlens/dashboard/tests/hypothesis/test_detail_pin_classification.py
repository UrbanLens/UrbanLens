"""Tests for automatic building classification of sub-markers.

The detail-pin dialog's Type select defaults to "Auto", which submits a blank
``pin_type``. These tests pin down what that blank means end to end: a
provisional type now, a background classification task queued, and
``pin_type_is_user_provided`` left False so the classifier is allowed to act -
versus an explicit pick, which is recorded as the user's own and never
touched again.

Celery is mocked throughout; the classifier itself is covered by
test_site_scope.py.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.wiki.model import Wiki

_coord_counter = 0

_ENQUEUE = "urbanlens.dashboard.services.celery.safely_enqueue_task"


def _make_location(**kwargs) -> Location:
    global _coord_counter
    _coord_counter += 1
    kwargs.setdefault("latitude", 44.0 + _coord_counter * 0.001)
    kwargs.setdefault("longitude", -75.0 - _coord_counter * 0.001)
    return baker.make(Location, google_place=None, **kwargs)


class DetailPinCreationTypeTests(TestCase):
    """Creating a child pin: blank type means "detect it", an explicit one is final."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.parent = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="parent")
        self.url = reverse("pin.detail_pins", kwargs={"pin_slug": self.parent.slug})

    def _post(self, **body) -> Pin:
        payload = {"latitude": "44.5", "longitude": "-75.5", **body}
        with patch(_ENQUEUE) as self.mock_enqueue:
            response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        return Pin.objects.get(uuid=response.json()["uuid"])

    def test_blank_type_is_provisional_and_not_user_provided(self) -> None:
        pin = self._post(pin_type="")
        self.assertEqual(pin.pin_type, PinType.POINT_OF_INTEREST)
        self.assertFalse(pin.pin_type_is_user_provided)

    def test_blank_type_queues_classification(self) -> None:
        pin = self._post(pin_type="")
        self.assertEqual(self.mock_enqueue.call_args[0][1:], ("pin", pin.pk))

    def test_omitted_type_behaves_like_auto(self) -> None:
        pin = self._post()
        self.assertFalse(pin.pin_type_is_user_provided)
        self.mock_enqueue.assert_called_once()

    def test_an_explicit_type_is_recorded_as_the_users_own(self) -> None:
        pin = self._post(pin_type=PinType.ENTRANCE)
        self.assertEqual(pin.pin_type, PinType.ENTRANCE)
        self.assertTrue(pin.pin_type_is_user_provided)

    def test_an_explicit_type_does_not_queue_classification(self) -> None:
        self._post(pin_type=PinType.ENTRANCE)
        self.mock_enqueue.assert_not_called()

    def test_a_bogus_type_falls_back_to_auto_rather_than_being_stored(self) -> None:
        pin = self._post(pin_type="not-a-real-type")
        self.assertEqual(pin.pin_type, PinType.POINT_OF_INTEREST)
        self.assertFalse(pin.pin_type_is_user_provided)


class DetailPinEditTypeTests(TestCase):
    """Editing a child pin: an omitted type is left entirely alone."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.parent = baker.make(Pin, profile=self.user.profile, location=_make_location(), slug="parent")
        self.child = baker.make(
            Pin,
            profile=self.user.profile,
            parent_pin=self.parent,
            location=_make_location(),
            pin_type=PinType.BUILDING,
            pin_type_is_user_provided=False,
        )
        self.url = reverse("pin.detail_pin.edit", kwargs={"pin_slug": self.parent.slug, "detail_pin_uuid": self.child.uuid})

    def _post(self, **body):
        with patch(_ENQUEUE) as self.mock_enqueue:
            response = self.client.post(self.url, data=json.dumps(body), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.child.refresh_from_db()

    def test_an_unrelated_edit_never_touches_the_type(self) -> None:
        """Restyling an auto-classified pin must not freeze that guess as a user choice."""
        self._post(color="#ff0000")
        self.assertEqual(self.child.pin_type, PinType.BUILDING)
        self.assertFalse(self.child.pin_type_is_user_provided)
        self.mock_enqueue.assert_not_called()

    def test_picking_a_type_records_it_as_the_users_own(self) -> None:
        self._post(pin_type=PinType.ENTRANCE)
        self.assertEqual(self.child.pin_type, PinType.ENTRANCE)
        self.assertTrue(self.child.pin_type_is_user_provided)
        self.mock_enqueue.assert_not_called()

    def test_switching_back_to_auto_re_derives_the_type(self) -> None:
        self._post(pin_type="")
        self.assertFalse(self.child.pin_type_is_user_provided)
        self.assertEqual(self.mock_enqueue.call_args[0][1:], ("pin", self.child.pk))

    def test_moving_an_auto_typed_pin_reclassifies_it(self) -> None:
        """It may have moved onto - or off - a building."""
        self._post(latitude="44.9", longitude="-75.9")
        self.assertEqual(self.mock_enqueue.call_args[0][1:], ("pin", self.child.pk))

    def test_moving_a_user_typed_pin_does_not_reclassify_it(self) -> None:
        Pin.objects.filter(pk=self.child.pk).update(pin_type_is_user_provided=True)
        self._post(latitude="44.91", longitude="-75.91")
        self.mock_enqueue.assert_not_called()


class ChildWikiClassificationTests(TestCase):
    """Community sub-markers follow the same rules as personal ones."""

    def setUp(self) -> None:
        super().setUp()
        self.user = baker.make(User)
        self.client.force_login(self.user)
        self.location = _make_location()
        self.wiki = baker.make(Wiki, location=self.location, name="Campus", slug="campus")
        # Wiki visibility requires the viewer to have a pin at the location.
        baker.make(Pin, profile=self.user.profile, location=self.location)
        self.url = reverse("location.wiki.detail_pins.panel", kwargs={"location_slug": self.location.slug})

    def _post(self, **body):
        payload = {"latitude": "44.6", "longitude": "-75.6", **body}
        with patch(_ENQUEUE) as self.mock_enqueue:
            response = self.client.post(self.url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        return Wiki.objects.get(uuid=response.json()["uuid"])

    def test_blank_type_queues_classification_for_the_child_wiki(self) -> None:
        child = self._post(pin_type="")
        self.assertFalse(child.pin_type_is_user_provided)
        self.assertEqual(self.mock_enqueue.call_args[0][1:], ("wiki", child.pk))

    def test_an_explicit_type_is_final_for_a_child_wiki_too(self) -> None:
        child = self._post(pin_type=PinType.ENTRANCE)
        self.assertEqual(child.pin_type, PinType.ENTRANCE)
        self.assertTrue(child.pin_type_is_user_provided)
        self.mock_enqueue.assert_not_called()


class ClassifyDetailMarkerTaskTests(TestCase):
    """The Celery task's own guards."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("dashboard.Profile")

    def test_a_missing_marker_is_a_quiet_no_op(self) -> None:
        from urbanlens.dashboard.tasks import classify_detail_marker

        self.assertFalse(classify_detail_marker("pin", 10_000_000))

    def test_a_user_typed_marker_short_circuits_before_generating_boundaries(self) -> None:
        from urbanlens.dashboard.tasks import classify_detail_marker

        pin = baker.make(Pin, profile=self.profile, location=_make_location(), pin_type=PinType.ENTRANCE, pin_type_is_user_provided=True)
        with patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as mock_generate:
            self.assertFalse(classify_detail_marker("pin", pin.pk))
        mock_generate.assert_not_called()

    def test_boundaries_are_generated_before_classifying(self) -> None:
        from urbanlens.dashboard.tasks import classify_detail_marker

        pin = baker.make(Pin, profile=self.profile, location=_make_location(), pin_type=PinType.POINT_OF_INTEREST)
        with patch("urbanlens.dashboard.services.locations.boundaries.generate_location_boundaries") as mock_generate:
            classify_detail_marker("pin", pin.pk)
        mock_generate.assert_called_once_with(pin.location)
