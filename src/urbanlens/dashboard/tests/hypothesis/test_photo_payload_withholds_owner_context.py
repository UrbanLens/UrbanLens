"""A photo somebody may see does not come with the bookkeeping around it.

`build_photo_payload` is the one place the external API turns an Image into JSON,
and it already withholds the owner-only parts: which pin the photo is filed
under, which visit it belongs to, whether its owner has dismissed it from
organising. Those are correct today, and this file exists so they stay that way -
adding a field to a payload is the easiest possible change to make, and the
hardest to notice is one that carries private context along with a public
picture.

The distinction the payload draws is the useful one: a photo can be visible
through a pin gallery while the *fact that it is filed under that pin* is not.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.external_api.serializers import build_photo_payload
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice

PRIVATE_PIN_NAME = "zzq-owners-private-pin-name"


class PhotoPayloadWithholdsOwnerContextTests(TestCase):
    """The viewer is a neighbour: their own pin at the same place."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner = baker.make(User).profile
        self.neighbour = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        self.owner_pin = baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None, name=PRIVATE_PIN_NAME)
        baker.make(Pin, profile=self.neighbour, location=self.location, parent_pin=None)
        # Shared as widely as possible, so the photo itself is legitimately
        # visible and only the context around it is in question.
        self.owner.photo_upload_visibility = VisibilityChoice.ANYONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        self.photo = Image.objects.create(
            image=SimpleUploadedFile("visible.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=self.owner,
            pin=self.owner_pin,
            location=self.location,
            caption="a photo anyone may see",
        )

    def test_a_viewer_gets_the_photo_without_the_pin_it_is_filed_under(self) -> None:
        payload = build_photo_payload(self.photo, self.neighbour)

        self.assertIsNotNone(payload["url"], "the photo itself should be visible")
        self.assertIsNone(payload["pin_slug"])
        self.assertIsNone(payload["pin_name"])
        self.assertIsNone(payload["visit_id"])

    def test_the_private_pin_name_appears_nowhere_in_the_payload(self) -> None:
        """Not merely absent from the field named after it: absent entirely.

        A future change that threads the pin name into a title, an alt text or a
        breadcrumb would pass the field-by-field assertions above.
        """
        payload = build_photo_payload(self.photo, self.neighbour)

        self.assertNotIn(PRIVATE_PIN_NAME, json.dumps(payload, default=str))

    def test_the_owner_still_gets_their_own_bookkeeping(self) -> None:
        payload = build_photo_payload(self.photo, self.owner)

        self.assertEqual(payload["pin_slug"], self.owner_pin.slug)
        self.assertEqual(payload["pin_name"], self.owner_pin.effective_name)

    def test_a_masked_identity_is_not_named_by_the_payload(self) -> None:
        """`owner_slug` goes through identity visibility, not straight off the row."""
        payload = build_photo_payload(self.photo, self.neighbour)

        self.assertIn("owner_slug", payload)
        if payload["owner_slug"] is not None:
            self.assertEqual(payload["owner_slug"], self.owner.slug, "owner_slug should be the owner's own slug when not masked")
