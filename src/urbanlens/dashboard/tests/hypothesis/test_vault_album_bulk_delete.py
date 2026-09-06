"""A vault album had no bulk delete, and no comment saying why (P61).

`_attach_owner_action_urls` set `gallery_bulk_url` only for a `Pin` owner, so
inside a Vault album the Delete and Send-to-wiki buttons rendered `hidden`
forever - you had to leave the album and use the per-tile trash button one
photo at a time.

The three actions do not resolve the same way, which is why this is not one
change:

- **Delete** applies. A vault photo already has a per-photo delete
  (`PhotoActionView.delete`); only the bulk form was missing.
- **Send to wiki** cannot. The pin endpoint derives the wiki from
  `pin.location`; a vault album has no location, and the vault's own per-photo
  version takes a `location_slug` from a picker the bulk bar does not have.
- **Bulk share** cannot. It opens the *pin* share dialog.
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.profile.model import Profile


class VaultGalleryBulkViewTests(TestCase):
    """POST /vault/photos/bulk/"""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # the first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile: Profile = self.user.profile
        self.client.force_login(self.user)

    def _post(self, payload: dict):
        return self.client.post(reverse("vault.photos.bulk"), json.dumps(payload), content_type="application/json")

    def test_it_deletes_the_requested_photos(self) -> None:
        images = baker.make(Image, profile=self.profile, _quantity=3)

        response = self._post({"action": "delete", "image_ids": [images[0].pk, images[1].pk]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 2)
        self.assertEqual(list(Image.objects.filter(profile=self.profile).values_list("pk", flat=True)), [images[2].pk])

    def test_it_never_touches_another_profiles_photos(self) -> None:
        stranger = baker.make(User)
        theirs = baker.make(Image, profile=stranger.profile)

        response = self._post({"action": "delete", "image_ids": [theirs.pk]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 0)
        self.assertTrue(Image.objects.filter(pk=theirs.pk).exists())

    def test_a_photo_that_is_also_on_a_wiki_is_unlinked_not_destroyed(self) -> None:
        # Same rule the pin endpoint follows: a contributed photo belongs to the
        # wiki too, so deleting it from the vault must not take it off the wiki.
        wiki = baker.make("dashboard.Wiki", location=baker.make("dashboard.Location"))
        contributed = baker.make(Image, profile=self.profile, wiki=wiki)

        response = self._post({"action": "delete", "image_ids": [contributed.pk]})

        self.assertEqual(response.json(), {"deleted": 0, "unlinked": 1})
        self.assertTrue(Image.objects.filter(pk=contributed.pk).exists())

    def test_send_to_wiki_is_refused_with_a_reason(self) -> None:
        image = baker.make(Image, profile=self.profile)

        response = self._post({"action": "send_to_wiki", "image_ids": [image.pk]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("which wiki", response.json()["error"])

    def test_a_malformed_body_is_a_400_not_a_500(self) -> None:
        response = self.client.post(reverse("vault.photos.bulk"), "not json", content_type="application/json")

        self.assertEqual(response.status_code, 400)

    def test_it_requires_login(self) -> None:
        self.client.logout()

        response = self._post({"action": "delete", "image_ids": []})

        self.assertEqual(response.status_code, 302)


class VaultAlbumActionUrlTests(TestCase):
    """What the album panel hands the client for each owner kind."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_a_vault_album_panel_carries_a_bulk_url(self) -> None:
        response = self.client.get(reverse("vault.photos.albums"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-gallery-bulk-url="{reverse("vault.photos.bulk")}"')

    def test_a_vault_album_panel_carries_no_wiki_url(self) -> None:
        # Send-to-wiki needs a location the vault does not have. The button is
        # keyed off this rather than off the bulk url, so it stays hidden while
        # Delete appears.
        response = self.client.get(reverse("vault.photos.albums"))

        self.assertContains(response, 'data-gallery-wiki-url=""')
