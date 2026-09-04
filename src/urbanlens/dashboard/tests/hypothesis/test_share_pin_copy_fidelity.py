"""Accepting a share must copy the pin faithfully, not rebuild it from defaults.

``create_pin_from_share`` lists ~28 fields by hand and promises, in its own docstring,
to carry over "every user-visible property (name, icon, labels, notes, scores, security
indicators, photos)". Anything it forgets takes the model default instead, and nothing
about adding a new field to ``Pin`` updates this list.
"""

from __future__ import annotations

from django.contrib.auth.models import User

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.model import PinShare, PinShareStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_from_share


class SharedPinCopyFidelityTests(TestCase):
    """Properties of the shared place must survive the copy."""

    def setUp(self):
        super().setUp()
        self.sender = self._profile("pin-copy-sender")
        self.recipient = self._profile("pin-copy-recipient")
        self.location = Location.objects.create(latitude=41.2, longitude=-73.2)

    @staticmethod
    def _profile(username: str) -> Profile:
        user = User.objects.create_user(username=username)
        profile, _ = Profile.objects.get_or_create(user=user)
        return profile

    def _accept(self, pin: Pin, images: list[Image] | None = None) -> Pin:
        share = PinShare.objects.create(
            pin=pin,
            location=self.location,
            from_profile=self.sender,
            to_profile=self.recipient,
            status=PinShareStatus.PENDING,
        )
        if images:
            share.images.set(images)
        return create_pin_from_share(share)

    def test_a_user_chosen_pin_type_stays_protected_from_reclassification(self):
        # pin_type_is_user_provided is the only thing stopping the automatic
        # building/parcel classifier from overwriting pin_type. Copying the type but
        # not the flag hands the recipient a type the classifier is free to replace -
        # exactly what the flag exists to prevent. name_is_user_provided, the same
        # pattern for the same reason, is copied.
        pin = Pin.objects.create(
            profile=self.sender,
            location=self.location,
            name="Chosen type",
            pin_type="building",
            pin_type_is_user_provided=True,
        )

        copied = self._accept(pin)

        self.assertEqual(copied.pin_type, "building")
        self.assertTrue(copied.pin_type_is_user_provided)

    def test_an_inferred_pin_type_stays_unprotected(self):
        pin = Pin.objects.create(profile=self.sender, location=self.location, name="Inferred type", pin_type="building")

        copied = self._accept(pin)

        self.assertFalse(copied.pin_type_is_user_provided)

    def test_a_custom_uploaded_icon_does_not_survive_the_share(self):
        """An icon is how one person marked a place for themselves.

        This used to assert the opposite, on the reasoning that losing it "changes
        what the pin looks like". It does, and that is the intent: the recipient
        gets the place, not the sender's presentation of it.
        """
        pin = Pin.objects.create(
            profile=self.sender, location=self.location, name="Custom icon", custom_icon="icons/skull.png", icon="place"
        )

        copied = self._accept(pin)

        self.assertFalse(copied.custom_icon, "the recipient inherited the sender's uploaded icon")

    def test_the_plain_icon_does_not_travel_either(self):
        """Same reasoning as the uploaded one - it is a presentation choice."""
        pin = Pin.objects.create(profile=self.sender, location=self.location, name="Plain icon", icon="place")

        copied = self._accept(pin)

        self.assertFalse(copied.custom_icon)
        self.assertNotEqual(copied.icon, "place", "the recipient inherited the sender's icon choice")

    def test_the_indoor_outdoor_classification_travels(self):
        pin = Pin.objects.create(profile=self.sender, location=self.location, name="Indoors", indoor_outdoor="indoor")

        copied = self._accept(pin)

        self.assertEqual(copied.indoor_outdoor, "indoor")

    def test_the_cover_photo_points_at_the_recipients_copy_of_that_photo(self):
        # The photos are copied, so the pin's hero image should survive too - but it
        # must point at the recipient's own row, never the sender's.
        pin = Pin.objects.create(profile=self.sender, location=self.location, name="With cover")
        cover = Image.objects.create(pin=pin, location=self.location, profile=self.sender, image="photos/cover.jpg")
        other = Image.objects.create(pin=pin, location=self.location, profile=self.sender, image="photos/other.jpg")
        pin.cover_photo = cover
        pin.save(update_fields=["cover_photo", "updated"])

        copied = self._accept(pin, images=[cover, other])

        self.assertIsNotNone(copied.cover_photo)
        self.assertEqual(copied.cover_photo.profile_id, self.recipient.pk)
        self.assertEqual(copied.cover_photo.image.name, "photos/cover.jpg")
        self.assertNotEqual(copied.cover_photo_id, cover.pk)

    def test_a_cover_photo_the_sender_did_not_share_is_dropped(self):
        # Sharing a subset of the gallery must not leave the copy pointing at a photo
        # the recipient never received.
        pin = Pin.objects.create(profile=self.sender, location=self.location, name="Partial share")
        cover = Image.objects.create(pin=pin, location=self.location, profile=self.sender, image="photos/private.jpg")
        shared = Image.objects.create(pin=pin, location=self.location, profile=self.sender, image="photos/shared.jpg")
        pin.cover_photo = cover
        pin.save(update_fields=["cover_photo", "updated"])

        copied = self._accept(pin, images=[shared])

        self.assertIsNone(copied.cover_photo_id)

    def test_a_pin_shared_with_no_photos_has_no_cover(self):
        pin = Pin.objects.create(profile=self.sender, location=self.location, name="No photos")

        copied = self._accept(pin)

        self.assertIsNone(copied.cover_photo_id)

    def test_the_senders_pin_is_left_alone(self):
        pin = Pin.objects.create(
            profile=self.sender,
            location=self.location,
            name="Untouched",
            pin_type="building",
            pin_type_is_user_provided=True,
        )

        self._accept(pin)

        pin.refresh_from_db()
        self.assertEqual(pin.profile_id, self.sender.pk)
        self.assertTrue(pin.pin_type_is_user_provided)


def _copied_field_names() -> set[str]:
    """Field names ``create_pin_from_share`` passes to its main ``Pin.objects.create``.

    Read from the source rather than by running the copy, so the check does not depend
    on a fixture happening to exercise every branch.
    """
    import ast
    import inspect

    from urbanlens.dashboard.services.sharing import pin_sharing

    tree = ast.parse(inspect.getsource(pin_sharing))
    passed: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "create_pin_from_share"):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "create"):
                continue
            names = {keyword.arg for keyword in call.keywords if keyword.arg}
            # The location-only branch builds a bare pin from a handful of fields;
            # the property copy is the large one.
            if len(names) > len(passed):
                passed = names
    return passed


class SharedPinCopyCoversEveryFieldTests(TestCase):
    """Adding a field to ``Pin`` must not silently skip the share copy.

    Every bug in the class above came from the same place: the copy names its fields
    by hand, so a field added later is simply absent and takes the model default.
    Nothing in review connects "add a column to Pin" with "update a function in the
    sharing service". This is what connects them.
    """

    #: Fields the copy deliberately leaves off, and why. A new Pin field must be
    #: added here (with a reason) or to the copy itself - the test names it either way.
    NOT_COPIED = {
        "id": "surrogate key",
        "uuid": "identity of the new row, not the old one",
        "created": "auto_now_add",
        "updated": "auto_now",
        "slug": "regenerated for the new pin; reusing the sender's would collide",
        "last_visited": "the recipient has not been there",
        "last_viewed_at": "the recipient has not opened it",
        "unlogged_visit_dismissed": "recipient's own dismissal state",
        "restructure_offer_dismissed": "recipient's own dismissal state",
        "buildings_auto_nested_at": "the sweep is per-pin state; the recipient's copy gets its own",
        "wiki": "a cache of an explicit link; the new pin resolves its own",
        "inferred_source_share": "provenance is recorded via source_share on the new pin",
        "cover_photo": "set afterwards by _carry_cover_photo, pointing at the recipient's copy",
        # The owner's side of the line. A share carries what is true about the
        # *site*; how one person recorded, rated or decorated it stays with them.
        # Ruled by Jess 2026-08-23, field by field, after each was found travelling.
        "description": "the owner's personal notes - and nothing in the product lets somebody consent to passing them on",
        "vulnerability": "the owner's rating of the place, not a property of it",
        "danger": "the owner's rating of the place, not a property of it",
        "priority": "how much this place matters to its owner",
        "icon": "how the owner marked the place for themselves",
        "color": "how the owner marked the place for themselves",
        "custom_icon": "how the owner marked the place for themselves",
        "detail_bg_color": "the owner's styling of their own pin",
        "detail_bg_opacity": "the owner's styling of their own pin",
        "detail_border_color": "the owner's styling of their own pin",
        "detail_border_opacity": "the owner's styling of their own pin",
    }

    def test_every_pin_field_is_either_copied_or_deliberately_skipped(self):
        passed = _copied_field_names()
        # A floor, not a target: the guard exists to notice the copy being
        # restructured out from under this test, and the count legitimately fell
        # when the owner-side fields below stopped travelling.
        self.assertGreater(
            len(passed), 12, "could not find the property copy - has create_pin_from_share been restructured?"
        )

        concrete = {field.name for field in Pin._meta.get_fields() if getattr(field, "concrete", False)}
        unaccounted = sorted(concrete - passed - set(self.NOT_COPIED))

        self.assertEqual(
            unaccounted,
            [],
            f"Pin fields that the share copy neither carries over nor explicitly skips: {unaccounted}.\n"
            "A field left out here silently takes its model default on the recipient's pin - which is how "
            "shared videos became photos and user-chosen pin types lost their protection.\n"
            "Either add it to create_pin_from_share, or add it to NOT_COPIED with the reason it should not travel.",
        )

    def test_the_skip_list_has_no_stale_entries(self):
        concrete = {field.name for field in Pin._meta.get_fields() if getattr(field, "concrete", False)}
        stale = sorted(set(self.NOT_COPIED) - concrete)

        self.assertEqual(stale, [], f"NOT_COPIED names fields Pin no longer has; delete them: {stale}")

    def test_the_skip_list_does_not_claim_fields_that_are_actually_copied(self):
        """An entry saying a field is skipped when it is not is a false statement in
        the one place someone would go to check."""
        passed = _copied_field_names()
        contradicted = sorted(set(self.NOT_COPIED) & passed)

        self.assertEqual(
            contradicted, [], f"NOT_COPIED claims these are skipped, but the copy passes them: {contradicted}"
        )
