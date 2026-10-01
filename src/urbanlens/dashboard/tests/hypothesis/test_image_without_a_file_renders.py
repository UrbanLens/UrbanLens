"""A photo row whose stored file is missing must not take a page down with it."""

from __future__ import annotations

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile


class ImageWithNoStoredFileTests(TestCase):
    """Every surface that lists photos, given a row whose file never landed."""

    def setUp(self) -> None:
        baker.make("auth.User")  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.pin = baker.make(Pin, profile=self.profile, name="Mill", name_is_user_provided=True)
        self.client.force_login(self.user)

    def _fileless_image(self, **kwargs) -> Image:
        # `image=""` is the state a failed download leaves behind: the row and
        # its source_url survive, the file does not.
        return baker.make(Image, profile=self.profile, image="", source_url="https://example.test/photo.jpg", **kwargs)

    def test_the_visit_history_panel_survives_one(self) -> None:
        visit = baker.make("dashboard.PinVisit", pin=self.pin)
        self._fileless_image(pin=self.pin, visit=visit)

        response = self.client.get(reverse("pin.visits", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "visit-photo-thumb")

    def test_the_pin_share_dialog_survives_one(self) -> None:
        # The dialog only renders its photo grid for an account that has
        # somebody to share with.
        friend = baker.make("auth.User")
        Friendship.objects.create(
            from_profile=self.profile, to_profile=Profile.objects.get(user=friend), status=FriendshipStatus.ACCEPTED
        )
        image = self._fileless_image(pin=self.pin)

        response = self.client.get(reverse("pin.share.dialog", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        # Asserted, not assumed: a fixture the list never reaches would make
        # this test pass against the unfixed template.
        self.assertContains(response, f'id="pin-share-photo-{image.pk}"')

    def test_the_profile_photo_strip_survives_one(self) -> None:
        # The strip lists only photos attached to a wiki or a DM - a pin-only
        # upload stays fully private and never reaches this template at all.
        wiki = baker.make("dashboard.Wiki", location=baker.make("dashboard.Location"))
        image = self._fileless_image(wiki=wiki)

        response = self.client.get(reverse("profile.view"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="profile-photo-{image.pk}"')

    def test_the_private_pin_page_survives_one_as_its_cover(self) -> None:
        # The hero reads pin.cover_photo.image.url. A missing cover_photo is
        # already fine - Django resolves an attribute off None to "" - so only
        # a cover row whose file is gone reaches this.
        self.pin.cover_photo = self._fileless_image(pin=self.pin)
        self.pin.save(update_fields=["cover_photo"])

        response = self.client.get(reverse("pin.details", args=[self.pin.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pin-detail-hero"')
