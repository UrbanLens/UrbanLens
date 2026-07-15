"""Regression tests for UL-150: pins imported with a new "create category" label

must actually be added to that label, not just create it unattached.
"""
from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.google.maps import GoogleMapsGateway
from urbanlens.dashboard.services.text_limits import MAX_PIN_DESCRIPTION_LENGTH


class ImportPreviewStreamingLabelAssignmentTests(TestCase):
    """GoogleMapsGateway.import_preview_streaming() attaches labels to imported pins."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _run(self, confirmed_lists: list[dict]) -> list[dict]:
        return list(self.gateway.import_preview_streaming(confirmed_lists, self.profile, auto_tag=False))

    def test_newly_created_category_label_is_attached_to_pin(self) -> None:
        self._run(
            [
                {
                    "stem": "Steel Mills",
                    "create_category": True,
                    "label_ids": [],
                    "pins": [{"name": "Old Steel Mill", "lat": 40.0, "lng": -74.0, "description": ""}],
                },
            ],
        )

        label = Label.objects.get(profile=self.profile, name__iexact="Steel Mills", kind="category")
        self.assertEqual(label.pins.count(), 1)
        self.assertEqual(label.pins.first().name, "Old Steel Mill")

    def test_existing_selected_label_is_still_attached(self) -> None:
        existing_label = baker.make(Label, profile=self.profile, name="Urbex", kind="tag")

        self._run(
            [
                {
                    "stem": "",
                    "create_category": False,
                    "label_ids": [existing_label.pk],
                    "pins": [{"name": "Old Factory", "lat": 41.0, "lng": -75.0, "description": ""}],
                },
            ],
        )

        existing_label.refresh_from_db()
        self.assertEqual(existing_label.pins.count(), 1)
        self.assertEqual(existing_label.pins.first().name, "Old Factory")

    def test_both_new_category_and_existing_label_are_attached_to_same_pin(self) -> None:
        existing_label = baker.make(Label, profile=self.profile, name="Urbex", kind="tag")

        self._run(
            [
                {
                    "stem": "Power Plants",
                    "create_category": True,
                    "label_ids": [existing_label.pk],
                    "pins": [{"name": "Old Power Plant", "lat": 42.0, "lng": -76.0, "description": ""}],
                },
            ],
        )

        category_label = Label.objects.get(profile=self.profile, name__iexact="Power Plants", kind="category")
        pin = category_label.pins.first()
        self.assertIsNotNone(pin)
        self.assertIn(existing_label, pin.labels.all())
        self.assertIn(category_label, pin.labels.all())


class ImportPreviewDescriptionLengthTests(TestCase):
    """_preview_pins() must not silently truncate descriptions that later get saved verbatim.

    The confirm/save step (import_preview_streaming) re-uses the exact dict
    _preview_pins() built for the client-facing preview - it never re-parses the
    original file. A tight, display-oriented cutoff there used to permanently
    truncate every imported pin's description to 500 characters, even though the
    preview UI never actually displays the description at all.
    """

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def test_preview_pins_keeps_a_long_description_intact(self) -> None:
        long_description = "x" * 2000
        raw_pins = [{"latitude": 40.0, "longitude": -74.0, "name": "Old Mill", "description": long_description}]
        preview = GoogleMapsGateway._preview_pins(raw_pins)
        self.assertEqual(preview[0]["description"], long_description)

    def test_preview_pins_clamps_at_the_real_max_length_not_500(self) -> None:
        huge_description = "x" * (MAX_PIN_DESCRIPTION_LENGTH + 1000)
        raw_pins = [{"latitude": 40.0, "longitude": -74.0, "name": "Old Mill", "description": huge_description}]
        preview = GoogleMapsGateway._preview_pins(raw_pins)
        self.assertEqual(len(preview[0]["description"]), MAX_PIN_DESCRIPTION_LENGTH)

    def test_a_long_description_survives_the_full_preview_then_confirm_flow(self) -> None:
        long_description = "A" * 2000 + " full KMZ description text that must not be cut off."
        raw_pins = [{"latitude": 40.0, "longitude": -74.0, "name": "Old Mill", "description": long_description}]
        preview = GoogleMapsGateway._preview_pins(raw_pins)

        list(
            self.gateway.import_preview_streaming(
                [{"stem": "", "create_category": False, "label_ids": [], "pins": preview}],
                self.profile,
                auto_tag=False,
            ),
        )

        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertEqual(pin.description, long_description)


class ImportPreviewDescriptionExtrasTests(TestCase):
    """HTML in a KMZ description is stripped, and any <img>/link URLs it embeds
    are turned into a Pin photo / PinLink - but only for pins the import
    actually creates, never for a pin it merges into."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = baker.make("auth.User").profile
        self.gateway = GoogleMapsGateway(api_key="test-key")

    def _run(self, description: str, *, name: str = "Old Mill", lat: float = 40.0, lng: float = -74.0):
        return list(
            self.gateway.import_preview_streaming(
                [{"stem": "", "create_category": False, "label_ids": [], "pins": [{"name": name, "lat": lat, "lng": lng, "description": description}]}],
                self.profile,
                auto_tag=False,
            ),
        )

    def test_html_is_stripped_from_the_saved_description(self) -> None:
        self._run('<img src="https://example.com/a.jpg"><br><br>City: Poughkeepsie<br>State: NY')
        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertNotIn("<img", pin.description)
        self.assertIn("City: Poughkeepsie", pin.description)

    def test_anchor_link_becomes_a_pin_link(self) -> None:
        self._run('Read more: <a href="https://example.com/story">here</a>')
        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertTrue(pin.links.filter(url="https://example.com/story").exists())

    def test_bare_url_becomes_a_pin_link(self) -> None:
        self._run("Tour: https://example.com/story")
        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertTrue(pin.links.filter(url="https://example.com/story").exists())

    def test_img_src_becomes_a_pin_photo_not_a_link(self) -> None:
        fake_image = mock.Mock(pin_id=None)
        with mock.patch(
            "urbanlens.dashboard.services.media_materialize.materialize_media_item",
            return_value=fake_image,
        ) as materialize:
            self._run('<img src="https://example.com/a.jpg">')
        materialize.assert_called_once()
        self.assertEqual(materialize.call_args.kwargs["url"], "https://example.com/a.jpg")
        fake_image.save.assert_called_once_with(update_fields=["pin", "updated"])
        pin = Pin.objects.get(profile=self.profile, name="Old Mill")
        self.assertEqual(pin.links.count(), 0)
        self.assertEqual(fake_image.pin, pin)

    def test_extras_are_not_applied_when_merging_into_an_existing_pin(self) -> None:
        existing, _created = Pin.objects.get_nearby_or_create(40.0, -74.0, self.profile, defaults={"name": "Existing Pin"})
        self._run('<a href="https://example.com/story">link</a>', name="Old Mill", lat=40.0, lng=-74.0)
        existing.refresh_from_db()
        self.assertEqual(existing.links.count(), 0)
        self.assertEqual(existing.name, "Existing Pin")
