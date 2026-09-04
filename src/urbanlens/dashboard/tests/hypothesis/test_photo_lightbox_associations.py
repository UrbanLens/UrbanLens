"""Tests for the shared lightbox's Batch 4 additions: pin/wiki/album association
display and the "File to a pin" / "Send to a wiki" / "Share with a friend"
actions (services.media.images.image_associations, PhotoAssociationsView,
PhotoWikiSearchView, PhotoShareFriendsView, and PhotoActionView's
send-to-wiki/share actions in controllers.vault_photos).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.album.model import Album, AlbumItem
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image, MediaKind
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.services.media.images import image_associations

_coord_counter = 0


def _location(**kwargs) -> Location:
    """A Location with per-call unique coordinates (Location is unique per lat/lng)."""
    global _coord_counter
    _coord_counter += 1
    return baker.make(
        Location, latitude=40.0 + _coord_counter * 0.001, longitude=-74.0 - _coord_counter * 0.001, **kwargs
    )


def _wiki_with_pin(profile, **wiki_kwargs) -> Wiki:
    """A Wiki whose location `profile` has a pin on (so it's wiki-visible to them)."""
    location = _location()
    wiki = baker.make(Wiki, location=location, **wiki_kwargs)
    baker.make(Pin, profile=profile, location=location)
    return wiki


def _make_accepted_friendship(a, b) -> Friendship:
    return Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class ImageAssociationsTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile

    def test_unfiled_photo_has_no_pin_or_wiki(self) -> None:
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        result = image_associations(image, self.profile)
        self.assertIsNone(result["pin"])
        self.assertIsNone(result["wiki"])
        self.assertEqual(result["albums"], [])

    def test_pin_filed_photo_reports_the_pin(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location(), name="Old Mill")
        image = baker.make(Image, profile=self.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        result = image_associations(image, self.profile)
        self.assertIsNotNone(result["pin"])
        self.assertIn("Old Mill", result["pin"]["name"])
        self.assertEqual(result["pin"]["url"], reverse("pin.details", args=[pin.slug]))

    def test_wiki_filed_photo_reports_the_wiki(self) -> None:
        wiki = _wiki_with_pin(self.profile, name="Sunset Ridge")
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        result = image_associations(image, self.profile)
        self.assertEqual(
            result["wiki"], {"name": "Sunset Ridge", "url": reverse("location.wiki", args=[wiki.location.slug])}
        )

    def test_reports_pin_album_membership(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location(), name="Old Mill")
        album = Album.objects.create(name="Interior", profile=self.profile, parent_pin=pin)
        image = baker.make(Image, profile=self.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        AlbumItem.objects.create(album=album, image=image)
        albums = image_associations(image, self.profile)["albums"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["name"], "Interior")
        self.assertEqual(albums[0]["owner_label"], "Pin album")
        self.assertEqual(albums[0]["owner_name"], "Old Mill")
        self.assertEqual(albums[0]["url"], reverse("pin.albums.detail", args=[pin.slug, album.slug]))

    def test_reports_vault_album_membership(self) -> None:
        album = Album.objects.create(name="Favorites", profile=self.profile, parent_profile=self.profile)
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        AlbumItem.objects.create(album=album, image=image)
        albums = image_associations(image, self.profile)["albums"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["owner_label"], "Vault album")
        self.assertEqual(albums[0]["owner_name"], "")
        self.assertEqual(albums[0]["url"], reverse("vault.photos.albums.detail", args=[album.slug]))

    def test_reports_wiki_album_membership(self) -> None:
        wiki = _wiki_with_pin(self.profile, name="Sunset Ridge")
        album = Album.objects.create(name="Community shots", profile=self.profile, parent_wiki=wiki)
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        AlbumItem.objects.create(album=album, image=image)
        albums = image_associations(image, self.profile)["albums"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["owner_label"], "Wiki album")
        self.assertEqual(albums[0]["owner_name"], "Sunset Ridge")
        self.assertEqual(
            albums[0]["url"], reverse("location.wiki.albums.detail", args=[wiki.location.slug, album.slug])
        )

    def test_wiki_filing_is_omitted_for_a_concealed_viewer(self) -> None:
        from unittest.mock import patch

        wiki = _wiki_with_pin(self.profile, name="Sunset Ridge")
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        with patch("urbanlens.dashboard.services.photos.albums._owner_conceal", return_value=True) as mock_conceal:
            result = image_associations(image, self.profile)
        mock_conceal.assert_called_once_with(wiki, self.profile)
        self.assertIsNone(result["wiki"])

    def test_concealed_wiki_album_is_omitted_for_a_concealed_viewer(self) -> None:
        from unittest.mock import patch

        # The photo itself is NOT on this wiki (wiki=None) - only the album
        # is (parent_wiki=old_wiki) - a real, reachable state, since
        # send-to-wiki repoints image.wiki without touching pre-existing
        # AlbumItem rows from a previous wiki. Proves the concealment check
        # is keyed on the album's own parent_wiki, not image.wiki (which here
        # is None - a mutation checking the wrong object would call
        # _owner_conceal(None, viewer) instead and this assertion would fail).
        old_wiki = _wiki_with_pin(self.profile, name="Old Ridge")
        album = Album.objects.create(name="Community shots", profile=self.profile, parent_wiki=old_wiki)
        image = baker.make(Image, profile=self.profile, wiki=None, pin=None, media_type=MediaKind.PHOTO)
        AlbumItem.objects.create(album=album, image=image)
        with patch("urbanlens.dashboard.services.photos.albums._owner_conceal", return_value=True) as mock_conceal:
            albums = image_associations(image, self.profile)["albums"]
        mock_conceal.assert_called_once_with(old_wiki, self.profile)
        self.assertEqual(albums, [])


class PhotoAssociationsViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_owner_sees_the_panel_even_for_a_completely_unfiled_photo(self) -> None:
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.photos.associations", args=[image.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "File to a pin")
        self.assertContains(response, "Send to a wiki")

    def test_filed_photo_shows_the_pin_link_instead_of_the_file_button(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location(), name="Old Mill")
        image = baker.make(Image, profile=self.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.photos.associations", args=[image.pk]))
        self.assertContains(response, "Old Mill")
        self.assertNotContains(response, "File to a pin")

    def test_wiki_filed_photo_shows_the_wiki_link_and_change_wiki_label(self) -> None:
        wiki = _wiki_with_pin(self.profile, name="Sunset Ridge")
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.photos.associations", args=[image.pk]))
        self.assertContains(response, "Sunset Ridge")
        self.assertContains(response, reverse("location.wiki", args=[wiki.location.slug]))
        self.assertContains(response, "Change wiki")
        self.assertNotContains(response, "Send to a wiki")

    def test_album_membership_renders_name_owner_and_link(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location(), name="Old Mill")
        album = Album.objects.create(name="Interior", profile=self.profile, parent_pin=pin)
        image = baker.make(Image, profile=self.profile, pin=pin, wiki=None, media_type=MediaKind.PHOTO)
        AlbumItem.objects.create(album=album, image=image)
        response = self.client.get(reverse("vault.photos.associations", args=[image.pk]))
        self.assertContains(response, "Interior")
        self.assertContains(response, "Pin album")
        self.assertContains(response, "Old Mill")
        self.assertContains(response, reverse("pin.albums.detail", args=[pin.slug, album.slug]))

    def test_non_owner_gets_204(self) -> None:
        other_profile = baker.make(User).profile
        image = baker.make(Image, profile=other_profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.photos.associations", args=[image.pk]))
        self.assertEqual(response.status_code, 204)


class PhotoPinSearchViewLightboxModeTests(TestCase):
    """The lightbox=1 branch _pin_search_results.html adds for the lightbox's
    "File to a pin" picker - distinct from the organize queue's own hx-post
    branch, which has no #photo-card-<id> to swap into inside the lightbox.
    """

    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_lightbox_mode_renders_a_data_pin_slug_button(self) -> None:
        pin = baker.make(Pin, profile=self.profile, location=_location(), name="Old Mill")
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.get(
            reverse("vault.photos.pin_search"), {"q": "Old Mill", "image_id": image.pk, "lightbox": "1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Old Mill")
        self.assertContains(response, f'data-pin-slug="{pin.slug}"')
        self.assertContains(response, "window._lightboxPickPin(this)")
        self.assertNotContains(response, "hx-post")

    def test_non_lightbox_mode_renders_the_hx_post_button(self) -> None:
        baker.make(Pin, profile=self.profile, location=_location(), name="Old Mill")
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.get(reverse("vault.photos.pin_search"), {"q": "Old Mill", "image_id": image.pk})
        self.assertContains(response, "hx-post")
        self.assertNotContains(response, "window._lightboxPickPin(this)")


class PhotoWikiSearchViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_finds_a_wiki_the_user_has_access_to(self) -> None:
        _wiki_with_pin(self.profile, name="Sunset Ridge Distillery")
        response = self.client.get(reverse("vault.photos.wiki_search"), {"q": "Sunset Ridge"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sunset Ridge Distillery")

    def test_short_query_returns_no_results(self) -> None:
        _wiki_with_pin(self.profile, name="Sunset Ridge Distillery")
        response = self.client.get(reverse("vault.photos.wiki_search"), {"q": "s"})
        self.assertNotContains(response, "Sunset Ridge Distillery")

    def test_inaccessible_wiki_is_not_found(self) -> None:
        # No pin at this wiki's location for self.profile - not visible to them.
        baker.make(Wiki, location=_location(), name="Somewhere Else")
        response = self.client.get(reverse("vault.photos.wiki_search"), {"q": "Somewhere Else"})
        self.assertNotContains(response, "Somewhere Else")


class PhotoShareFriendsViewTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_lists_accepted_friends(self) -> None:
        friend = baker.make(User, username="pal").profile
        _make_accepted_friendship(self.profile, friend)
        response = self.client.get(reverse("vault.photos.share_friends"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pal")

    def test_excludes_a_non_friend(self) -> None:
        baker.make(User, username="stranger")
        response = self.client.get(reverse("vault.photos.share_friends"))
        self.assertNotContains(response, "stranger")


class SendToWikiActionTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def _action_url(self, image: Image, action: str) -> str:
        return reverse("vault.photos.action", args=[image.pk, action])

    def test_files_an_unfiled_photo_onto_the_wiki(self) -> None:
        wiki = _wiki_with_pin(self.profile)
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.post(self._action_url(image, "send-to-wiki"), {"location_slug": wiki.location.slug})
        self.assertEqual(response.status_code, 200)
        image.refresh_from_db()
        self.assertEqual(image.wiki_id, wiki.pk)

    def test_can_move_to_a_different_wiki(self) -> None:
        old_wiki = _wiki_with_pin(self.profile)
        new_wiki = _wiki_with_pin(self.profile)
        image = baker.make(Image, profile=self.profile, wiki=old_wiki, pin=None, media_type=MediaKind.PHOTO)
        self.client.post(self._action_url(image, "send-to-wiki"), {"location_slug": new_wiki.location.slug})
        image.refresh_from_db()
        self.assertEqual(image.wiki_id, new_wiki.pk)

    def test_resending_to_the_same_wiki_is_a_no_op_info_toast(self) -> None:
        from urbanlens.dashboard.models.images.attachment import ImageAttachment

        wiki = _wiki_with_pin(self.profile)
        image = baker.make(Image, profile=self.profile, wiki=wiki, pin=None, media_type=MediaKind.PHOTO)
        attachments_before = ImageAttachment.objects.filter(image=image, wiki=wiki).count()
        response = self.client.post(self._action_url(image, "send-to-wiki"), {"location_slug": wiki.location.slug})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Already on that wiki.", response.headers.get("HX-Trigger", ""))
        image.refresh_from_db()
        self.assertEqual(image.wiki_id, wiki.pk)
        self.assertEqual(ImageAttachment.objects.filter(image=image, wiki=wiki).count(), attachments_before)

    def test_inaccessible_wiki_is_refused(self) -> None:
        other_profile = baker.make(User).profile
        wiki = _wiki_with_pin(other_profile)
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.post(self._action_url(image, "send-to-wiki"), {"location_slug": wiki.location.slug})
        self.assertEqual(response.status_code, 200)
        image.refresh_from_db()
        self.assertIsNone(image.wiki_id)

    def test_cannot_act_on_another_profiles_photo(self) -> None:
        other_profile = baker.make(User).profile
        wiki = _wiki_with_pin(self.profile)
        image = baker.make(Image, profile=other_profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.post(self._action_url(image, "send-to-wiki"), {"location_slug": wiki.location.slug})
        self.assertEqual(response.status_code, 404)


def _upload_real_photo(owner, profile, name="photo.jpg"):
    """A real uploaded Image with an actual stored file.

    Needed anywhere the share action's DM broadcast path runs for real (it
    serializes the attached image's .image.url) - a bare baker.make(Image)
    row has no file behind it and raises there, which a bare baker fixture
    would not reveal since it's not what any real share() call ever acts on.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from urbanlens.core.tests.images import JPEG_BYTES
    from urbanlens.dashboard.services.photos.uploads import upload_photo_for_owner

    image = upload_photo_for_owner(owner, profile, SimpleUploadedFile(name, JPEG_BYTES, content_type="image/jpeg"))
    assert isinstance(image, Image)
    return image


class ShareActionTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = self.user.profile
        self.friend_user: User = baker.make(User, username="pal")
        self.friend = self.friend_user.profile
        _make_accepted_friendship(self.profile, self.friend)
        self.client.force_login(self.user)

    def _action_url(self, image: Image, action: str = "share") -> str:
        return reverse("vault.photos.action", args=[image.pk, action])

    def test_shares_a_never_attached_photo_directly(self) -> None:
        image = _upload_real_photo(self.profile, self.profile)
        response = self.client.post(self._action_url(image), {"friend_slug": self.friend.slug})
        self.assertEqual(response.status_code, 200)
        message = DirectMessage.objects.get(sender=self.profile, recipient=self.friend)
        self.assertTrue(Image.objects.filter(pk=image.pk, direct_message=message).exists())

    def test_sharing_the_same_never_attached_photo_twice_never_double_attaches_the_original(self) -> None:
        """Two share() calls for a photo that started unattached (the shape a
        race between two near-simultaneous clicks would produce) must not both
        attach the same original Image to two different messages - the
        second call has to see the first's attachment and make its own
        deduped copy instead. Guards the select_for_update fix in share().
        """
        image = _upload_real_photo(self.profile, self.profile)
        other_friend_user = baker.make(User, username="other_pal")
        other_friend = other_friend_user.profile
        _make_accepted_friendship(self.profile, other_friend)

        self.client.post(self._action_url(image), {"friend_slug": self.friend.slug})
        self.client.post(self._action_url(image), {"friend_slug": other_friend.slug})

        first_message = DirectMessage.objects.get(sender=self.profile, recipient=self.friend)
        second_message = DirectMessage.objects.get(sender=self.profile, recipient=other_friend)
        self.assertTrue(Image.objects.filter(pk=image.pk, direct_message=first_message).exists())
        second_image = Image.objects.get(direct_message=second_message)
        self.assertNotEqual(second_image.pk, image.pk)
        self.assertEqual(second_image.checksum, image.checksum)

    def test_sharing_an_already_attached_photo_sends_a_deduped_copy(self) -> None:
        from urbanlens.dashboard.services.messaging.direct_messages import create_direct_message

        first_friend_user = baker.make(User, username="earlier_friend")
        first_friend = first_friend_user.profile
        _make_accepted_friendship(self.profile, first_friend)
        image = _upload_real_photo(self.profile, self.profile)

        create_direct_message(self.profile, first_friend, "", image_ids=[image.pk])
        image.refresh_from_db()
        self.assertIsNotNone(image.direct_message_id)

        before_count = Image.objects.filter(profile=self.profile).count()
        response = self.client.post(self._action_url(image), {"friend_slug": self.friend.slug})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Image.objects.filter(profile=self.profile).count(), before_count + 1)
        message = DirectMessage.objects.get(sender=self.profile, recipient=self.friend)
        copy = Image.objects.get(direct_message=message)
        self.assertNotEqual(copy.pk, image.pk)
        self.assertEqual(copy.image.name, image.image.name)

    def test_unknown_friend_slug_is_refused(self) -> None:
        image = baker.make(Image, profile=self.profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.post(self._action_url(image), {"friend_slug": "not-a-real-slug"})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(DirectMessage.objects.filter(sender=self.profile).exists())

    def test_cannot_share_another_profiles_photo(self) -> None:
        other_profile = baker.make(User).profile
        image = baker.make(Image, profile=other_profile, pin=None, wiki=None, media_type=MediaKind.PHOTO)
        response = self.client.post(self._action_url(image), {"friend_slug": self.friend.slug})
        self.assertEqual(response.status_code, 404)
