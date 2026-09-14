"""The map payload's projection path must answer exactly what the model path does."""

from __future__ import annotations

import itertools

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.agreement import assert_agrees
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.labels.customization.model import LabelCustomization
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.map_pins import MapPinPayloadService


class MapPayloadPathAgreementTests(TestCase):
    """`all()` and `serialize()` must produce identical dicts for the same pins."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self._coordinate = 0
        self._build_labels()
        self._build_pins()

    def _next_location(self, **fields: object) -> Location:
        """A location at coordinates nothing else has used (they are unique together)."""
        self._coordinate += 1
        return baker.make(
            Location,
            latitude=f"41.{self._coordinate:06d}",
            longitude=f"-73.{self._coordinate:06d}",
            **fields,
        )

    def _build_labels(self) -> None:
        # Label is unique on (lower(name), profile, kind) and the app seeds
        # global defaults, so these names are prefixed to stay clear of them.
        self.plain_tag = baker.make(Label, kind="tag", name="Agreement Plain", icon="star", color="#ff0000", order=1)
        self.category = baker.make(
            Label, kind="category", name="Agreement Ruins", icon="home", color="#00ff00", order=5
        )
        self.status = baker.make(Label, kind="status", name="Agreement Demolished", icon=None, color=None, order=2)
        # Higher order, so it wins the icon race against plain_tag.
        self.image_label = baker.make(Label, kind="tag", name="Agreement Imaged", icon=None, color="#0000ff", order=9)
        self.image_label.custom_icon = "label_icons/example.png"
        self.image_label.save(update_fields=["custom_icon"])
        # An override that clears the icon rather than replacing it: `icon` is
        # set, to "". A fallback written as `override or base` reads that as
        # absent and wrongly shows the base icon.
        self.cleared_label = baker.make(
            Label, kind="tag", name="Agreement Cleared", icon="bolt", color="#abcdef", order=7
        )
        LabelCustomization.objects.create(
            profile=self.profile, label=self.cleared_label, icon="", color="", name="Renamed"
        )

    def _build_pins(self) -> None:
        names = ["Named Pin", ""]
        label_sets: list[list[Label]] = [
            [],
            [self.plain_tag],
            [self.plain_tag, self.category, self.status],
            [self.image_label, self.plain_tag],
            [self.cleared_label],
        ]
        place_shapes = [
            {"official_name": "Official Place", "city": "Albany", "state": "NY", "country": ""},
            # No name at all, so display_name falls through to the area placeholder.
            {"official_name": None, "city": "Kyiv", "state": "", "country": "Ukraine"},
            # Nothing to build a placeholder from either.
            {"official_name": None, "city": "", "state": "", "country": ""},
        ]
        for index, (name, labels, place) in enumerate(itertools.product(names, label_sets, place_shapes)):
            # street_number/route, not address_basic - the latter is a read-only
            # property joining the two.
            street = {"street_number": "1", "route": "Main St"} if index % 2 else {}
            location = self._next_location(**street, **place)
            if index % 3 == 0:
                baker.make(Wiki, location=location, name=f"Wiki Name {index}")
            pin = baker.make(
                Pin,
                profile=self.profile,
                location=location,
                name=name,
                icon="place" if index % 4 == 0 else None,
                color="#123456" if index % 5 == 0 else None,
            )
            if labels:
                pin.labels.set(labels)
            if index % 6 == 0:
                pin.cover_photo = baker.make(Image, pin=pin, profile=self.profile, media_type=MediaKind.PHOTO)
                pin.save(update_fields=["cover_photo"])
            elif index % 6 == 1:
                # No cover photo, but a photo to fall back to.
                baker.make(Image, pin=pin, profile=self.profile, media_type=MediaKind.PHOTO)
            if index % 7 == 0:
                baker.make(Pin, profile=self.profile, parent_pin=pin, location=self._next_location())

    def test_the_projection_path_agrees_with_the_model_path(self) -> None:
        query = Pin.objects.filter(profile=self.profile).root_pins()

        model_service = MapPinPayloadService(self.profile)
        model_payloads = {pin.pk: model_service.serialize(pin) for pin in model_service.prepare_queryset(query)}
        projected = {payload["id"]: payload for payload in MapPinPayloadService(self.profile).all(query)}

        self.assertEqual(sorted(projected), sorted(model_payloads), "the two paths returned different pins")
        self.assertGreater(
            len(model_payloads), 20, "the generated matrix collapsed - it is no longer covering the branches"
        )
        assert_agrees(
            model_payloads.__getitem__,
            projected.__getitem__,
            sorted(model_payloads),
            describe=lambda pk: f"pin {pk} ({model_payloads[pk]['name']!r})",
            label="the projection path",
        )

    def test_page_agrees_with_all(self) -> None:
        """Paging must not change a payload, only which payloads come back."""
        query = Pin.objects.filter(profile=self.profile).root_pins()
        service = MapPinPayloadService(self.profile)

        everything = {payload["id"]: payload for payload in service.all(query)}
        paged: dict[int, dict[str, object]] = {}
        cursor: int | None = None
        while True:
            page = MapPinPayloadService(self.profile).page(query, cursor=cursor, limit=4)
            paged.update({payload["id"]: payload for payload in page.pins})
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        self.assertEqual(sorted(paged), sorted(everything))
        assert_agrees(everything.__getitem__, paged.__getitem__, sorted(everything), label="page()")
