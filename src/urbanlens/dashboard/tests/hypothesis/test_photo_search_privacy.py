"""Search must not return photos their uploader has not agreed to show you.

`PhotoSearchProvider` reaches other people's photos on purpose: its third
disjunct is ``Q(location__pins__profile=profile)``, "any image at a location I
have a pin at", which is how you find pictures of a place you follow. It did that
without consulting `ImageQuerySet.visible_to`, so a photo whose uploader had set
`photo_upload_visibility=NO_ONE` came back anyway - and a photo result carries
the caption, the *owning pin's name*, and a link to that pin, so what leaked was
not only the picture.

Every other search provider scopes to the searcher's own rows. This is the one
that does not, which is why it is the one that needed the filter.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice

_NONCE = "zzqqxx-private-caption"


class PhotoSearchRespectsUploaderVisibilityTests(TestCase):
    """A neighbour is someone with their own pin at the same place."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner = baker.make(User).profile
        self.neighbour = baker.make(User).profile
        self.location = baker.make(Location, latitude=41.7361, longitude=-73.9361)
        # Both have a pin at the same place: that is what makes the third
        # disjunct match, and it is an ordinary situation rather than an attack.
        self.owner_pin = baker.make(Pin, profile=self.owner, location=self.location, parent_pin=None, name="Owner's private survey")
        self.neighbour_pin = baker.make(Pin, profile=self.neighbour, location=self.location, parent_pin=None)
        self.photo = Image.objects.create(
            image=SimpleUploadedFile("secret.jpg", b"not-a-real-jpeg", content_type="image/jpeg"),
            profile=self.owner,
            pin=self.owner_pin,
            location=self.location,
            caption=_NONCE,
        )

    def _search(self, profile, term: str = _NONCE):
        """Run the real provider the search page uses."""
        from urbanlens.dashboard.services.global_search.parser import parse_query
        from urbanlens.dashboard.services.global_search.providers import PhotoSearchProvider

        return PhotoSearchProvider().search(profile, parse_query(term), 20)

    def test_a_photo_nobody_may_see_is_not_returned_to_a_neighbour(self) -> None:
        self.owner.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["photo_upload_visibility"])

        results = self._search(self.neighbour)

        self.assertEqual([r.title for r in results], [], "search returned a photo its uploader shows to no one")

    def test_the_owning_pin_name_does_not_leak_through_the_result(self) -> None:
        """A result's title falls back to the pin's name when there is no caption,
        so a photo with no caption at all still names somebody's private pin."""
        self.owner.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["photo_upload_visibility"])
        Image.objects.filter(pk=self.photo.pk).update(caption=None)

        results = self._search(self.neighbour, "survey")

        for result in results:
            self.assertNotIn("Owner's private survey", result.title)
            self.assertNotIn(self.owner_pin.slug or "", result.url)

    def test_the_owner_still_finds_their_own_photo(self) -> None:
        """The filter must not cost the searcher their own library."""
        self.owner.photo_upload_visibility = VisibilityChoice.NO_ONE
        self.owner.save(update_fields=["photo_upload_visibility"])

        results = self._search(self.owner)

        self.assertTrue(any(_NONCE in (r.title or "") for r in results), "the uploader lost their own photo from search")

    def test_a_photo_shared_widely_is_still_found_by_a_neighbour(self) -> None:
        """The positive control: this suite must not pass by breaking search."""
        self.owner.photo_upload_visibility = VisibilityChoice.ANYONE
        self.owner.save(update_fields=["photo_upload_visibility"])

        results = self._search(self.neighbour)

        self.assertTrue(any(_NONCE in (r.title or "") for r in results), "a photo shared with anyone stopped being findable")
