"""Tests for the external API's messaging surface.

The guards that matter most here, in rough order of how bad the failure would
be if they regressed:

- **Share provenance.** A pin shared through the API must record a
  ``LocationExposure``, because the API layer routes through the same
  ``create_pin_share`` the web composer uses. Constructing the share row
  directly would still "work" from the outside while silently leaving a hole in
  the re-share chain, so this is asserted on the database, not the response.
- **Identity masking.** A partner whose profile visibility masks them must not
  have their real username surface over the mobile API, where it would go
  unnoticed far longer than on the web.
- **Credential kind.** A PAT-style ``ApiKey`` can never reach messaging, even
  holding the scopes.
- **Idempotency.** A retried send returns the existing message rather than
  delivering it twice - and, for a share, without creating a second share.
- **Tombstones.** An expired/deleted message's content must not be served to
  the recipient just because a different surface asked for it.
"""

from __future__ import annotations

import base64
from datetime import timedelta
import json
import os

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
from oauth2_provider.models import get_access_token_model, get_application_model

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_share.exposure import LocationExposure
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.oauth_clients import FIRST_PARTY_CLIENT_ID
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.direct_messages import create_direct_message

#: A real 1x1 PNG - ImageField stores whatever bytes it's given, but the
#: upload pipeline sniffs content, so a valid file avoids testing the wrong
#: rejection path.
_PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

AccessToken = get_access_token_model()
Application = get_application_model()

READ_WRITE = f"{ApiKeyScope.MESSAGES_READ.value} {ApiKeyScope.MESSAGES_WRITE.value}"


def _bearer(raw: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _profile() -> Profile:
    return baker.make(User).profile


def _token_for(user: User, scope: str = READ_WRITE) -> str:
    token = AccessToken.objects.create(
        user=user,
        application=Application.objects.get(client_id=FIRST_PARTY_CLIENT_ID),
        token=f"tok-{os.urandom(8).hex()}",
        expires=timezone.now() + timedelta(hours=1),
        scope=scope,
    )
    return token.token


def _open_dms(*profiles: Profile) -> None:
    Profile.objects.filter(pk__in=[profile.pk for profile in profiles]).update(direct_message_visibility=VisibilityChoice.ANYONE)
    for profile in profiles:
        profile.refresh_from_db()


def _befriend(a: Profile, b: Profile) -> None:
    """Connect two profiles.

    Exactly one row, never one per direction: ``Friendship.objects.between()``
    resolves the pair with ``.get()``, so a second reciprocal row makes every
    connection check raise ``MultipleObjectsReturned``.
    """
    Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


class MessagingBaseTestCase(TestCase):
    """Two mutually-messageable profiles and an OAuth2 token for the sender."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # first user is auto-promoted to site admin
        self.sender = _profile()
        self.partner = _profile()
        _open_dms(self.sender, self.partner)
        self.token = _token_for(self.sender.user)
        self.auth = _bearer(self.token)

    def _thread_url(self, peer: Profile | None = None) -> str:
        return reverse("external_api:messages.thread", kwargs={"peer_slug": (peer or self.partner).ensure_slug()})

    def _post_json(self, url: str, payload: dict, **extra):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json", **{**self.auth, **extra})


class SendMessageTests(MessagingBaseTestCase):
    """Sending a plain message through the API."""

    def test_sends_a_plaintext_message(self) -> None:
        response = self._post_json(self._thread_url(), {"body": "on my way"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["body"], "on my way")
        self.assertTrue(DirectMessage.objects.filter(sender=self.sender, recipient=self.partner, body="on my way").exists())

    def test_empty_message_is_rejected(self) -> None:
        self.assertEqual(self._post_json(self._thread_url(), {"body": "   "}).status_code, 400)

    def test_plaintext_and_ciphertext_together_are_rejected(self) -> None:
        response = self._post_json(self._thread_url(), {"body": "hi", "ciphertext": "AAAA", "nonce": "BBBB", "key_version": 1})
        self.assertEqual(response.status_code, 400)

    def test_ciphertext_without_a_nonce_is_rejected(self) -> None:
        self.assertEqual(self._post_json(self._thread_url(), {"ciphertext": "AAAA", "key_version": 1}).status_code, 400)

    def test_ciphertext_with_key_version_zero_is_rejected(self) -> None:
        """Version 0 means plaintext - such a message could never be decrypted."""
        self.assertEqual(self._post_json(self._thread_url(), {"ciphertext": "AAAA", "nonce": "BBBB", "key_version": 0}).status_code, 400)

    def test_two_shares_at_once_are_rejected(self) -> None:
        response = self._post_json(self._thread_url(), {"body": "x", "shared_pin_id": "a", "shared_trip_slug": "b"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_peer_is_404(self) -> None:
        self.assertEqual(self._post_json(reverse("external_api:messages.thread", kwargs={"peer_slug": "nobody-here"}), {"body": "x"}).status_code, 404)


def _make_image(profile: Profile, **kwargs) -> Image:
    """Create an Image row owned by *profile* with a real stored file."""
    return Image.objects.create(image=SimpleUploadedFile("photo.png", _PNG_BYTES, content_type="image/png"), profile=profile, **kwargs)


class SendMessageAttachmentTests(MessagingBaseTestCase):
    """``image_uuids`` is additive alongside the pre-existing integer ``image_ids``."""

    def test_image_uuids_attaches_the_image(self) -> None:
        image = _make_image(self.sender)
        response = self._post_json(self._thread_url(), {"body": "look", "image_uuids": [str(image.uuid)]})
        self.assertEqual(response.status_code, 201)
        image.refresh_from_db()
        self.assertEqual(image.direct_message_id, DirectMessage.objects.get(sender=self.sender).pk)

    def test_image_ids_and_image_uuids_together_both_attach(self) -> None:
        by_id = _make_image(self.sender)
        by_uuid = _make_image(self.sender)
        response = self._post_json(self._thread_url(), {"body": "two photos", "image_ids": [by_id.pk], "image_uuids": [str(by_uuid.uuid)]})
        self.assertEqual(response.status_code, 201)
        message = DirectMessage.objects.get(sender=self.sender, body="two photos")
        self.assertEqual(set(Image.objects.filter(direct_message=message).values_list("pk", flat=True)), {by_id.pk, by_uuid.pk})

    def test_another_profiles_image_uuid_is_not_attached(self) -> None:
        """image_uuids is scoped to the sender's own images, matching image_ids."""
        foreign_image = _make_image(self.partner)
        response = self._post_json(self._thread_url(), {"body": "nice try", "image_uuids": [str(foreign_image.uuid)]})
        self.assertEqual(response.status_code, 201)
        foreign_image.refresh_from_db()
        self.assertIsNone(foreign_image.direct_message_id)


class CredentialKindTests(MessagingBaseTestCase):
    """Messaging is OAuth2-only, regardless of what an ApiKey claims."""

    def test_pat_api_key_is_refused_even_holding_messaging_scopes(self) -> None:
        key, raw = generate_api_key(self.sender.user, "leaky-key")
        ApiKey.objects.filter(pk=key.pk).update(scopes=[ApiKeyScope.MESSAGES_READ.value, ApiKeyScope.MESSAGES_WRITE.value])
        self.assertEqual(self.client.get(reverse("external_api:messages.conversations"), **_bearer(raw)).status_code, 403)

    def test_read_only_token_cannot_send(self) -> None:
        token = _token_for(self.sender.user, ApiKeyScope.MESSAGES_READ.value)
        response = self.client.post(self._thread_url(), data=json.dumps({"body": "x"}), content_type="application/json", **_bearer(token))
        self.assertEqual(response.status_code, 403)

    def test_read_only_token_cannot_mark_read(self) -> None:
        token = _token_for(self.sender.user, ApiKeyScope.MESSAGES_READ.value)
        url = reverse("external_api:messages.read", kwargs={"peer_slug": self.partner.ensure_slug()})
        self.assertEqual(self.client.post(url, **_bearer(token)).status_code, 403)

    def test_read_only_token_can_still_read(self) -> None:
        token = _token_for(self.sender.user, ApiKeyScope.MESSAGES_READ.value)
        self.assertEqual(self.client.get(self._thread_url(), **_bearer(token)).status_code, 200)

    def test_anonymous_is_refused(self) -> None:
        self.assertIn(self.client.get(reverse("external_api:messages.conversations")).status_code, (401, 403))


class IdempotencyTests(MessagingBaseTestCase):
    """A retried send resolves to the message that already exists."""

    def test_replayed_client_uuid_returns_the_existing_message(self) -> None:
        client_uuid = "6f1f4a4e-0b4a-4c1a-9a1e-3f4d5e6a7b8c"
        first = self._post_json(self._thread_url(), {"body": "only once", "client_uuid": client_uuid})
        second = self._post_json(self._thread_url(), {"body": "only once", "client_uuid": client_uuid})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200, "a replay is not a fresh create")
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(DirectMessage.objects.filter(sender=self.sender, recipient=self.partner).count(), 1)

    def test_distinct_client_uuids_both_send(self) -> None:
        self._post_json(self._thread_url(), {"body": "one", "client_uuid": "11111111-1111-4111-8111-111111111111"})
        self._post_json(self._thread_url(), {"body": "two", "client_uuid": "22222222-2222-4222-8222-222222222222"})
        self.assertEqual(DirectMessage.objects.filter(sender=self.sender).count(), 2)

    def test_a_send_without_a_client_uuid_is_never_deduplicated(self) -> None:
        self._post_json(self._thread_url(), {"body": "same text"})
        self._post_json(self._thread_url(), {"body": "same text"})
        self.assertEqual(DirectMessage.objects.filter(sender=self.sender).count(), 2)


class PinShareProvenanceTests(MessagingBaseTestCase):
    """An API-driven pin share records the exposure the web path records."""

    def setUp(self) -> None:
        super().setUp()
        _befriend(self.sender, self.partner)
        self.location = baker.make(Location, latitude=41.5, longitude=-73.5)
        self.pin = baker.make(Pin, profile=self.sender, location=self.location)

    def test_sharing_a_pin_records_a_location_exposure(self) -> None:
        """The single most important assertion in this module.

        A share built directly in the view would produce a working-looking
        card and no exposure row at all.
        """
        before = LocationExposure.objects.filter(profile=self.partner, location=self.location).count()
        response = self._post_json(self._thread_url(), {"body": "check this out", "shared_pin_id": self.pin.ensure_slug()})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["share"]["kind"], "pin")
        after = LocationExposure.objects.filter(profile=self.partner, location=self.location).count()
        self.assertEqual(after, before + 1, "sharing a pin must record the recipient's exposure to its location")

    def test_sharing_an_unknown_pin_is_404(self) -> None:
        self.assertEqual(self._post_json(self._thread_url(), {"body": "x", "shared_pin_id": "no-such-pin"}).status_code, 404)

    def test_cannot_share_someone_elses_pin(self) -> None:
        """Pin resolution is scoped to the sender's own pins.

        Addressed by uuid rather than slug on purpose: pin slugs are unique
        only *per profile* (``db_pin_unique_slug_per_profile``), so a slug is
        not a global identifier and resolving one already cannot escape the
        sender's own pins. The uuid is global, which makes this the real test
        of the ownership filter - naming another profile's pin unambiguously
        must still find nothing.
        """
        other_pin = baker.make(Pin, profile=self.partner, location=baker.make(Location, latitude=1.0, longitude=1.0))
        response = self._post_json(self._thread_url(), {"body": "x", "shared_pin_id": str(other_pin.uuid)})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            LocationExposure.objects.filter(location=other_pin.location).exists(),
            "a refused share must not have recorded any exposure",
        )

    def test_a_replayed_pin_share_does_not_double_the_exposure(self) -> None:
        client_uuid = "33333333-3333-4333-8333-333333333333"
        payload = {"body": "check this out", "shared_pin_id": self.pin.ensure_slug(), "client_uuid": client_uuid}
        self._post_json(self._thread_url(), payload)
        self._post_json(self._thread_url(), payload)

        self.assertEqual(DirectMessage.objects.filter(sender=self.sender).count(), 1)
        self.assertEqual(LocationExposure.objects.filter(profile=self.partner, location=self.location).count(), 1)


class IdentityMaskingTests(MessagingBaseTestCase):
    """A masked partner's real username never reaches the API."""

    def test_masked_partner_display_name_is_not_the_username(self) -> None:
        create_direct_message(self.partner, self.sender, "hello there")
        # The partner hides their profile from everyone; the conversation stays
        # readable, but their identity must be anonymized everywhere it shows.
        Profile.objects.filter(pk=self.partner.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        self.partner.refresh_from_db()

        payload = self.client.get(reverse("external_api:messages.conversations"), **self.auth).json()
        rows = [row for row in payload["results"] if row["kind"] == "dm"]
        self.assertTrue(rows)
        for row in rows:
            self.assertNotEqual(row["display_name"], self.partner.username)
            self.assertTrue(row["is_anonymized"])

    def test_masked_sender_name_is_not_leaked_in_the_thread(self) -> None:
        create_direct_message(self.partner, self.sender, "hello there")
        Profile.objects.filter(pk=self.partner.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        self.partner.refresh_from_db()

        results = self.client.get(self._thread_url(), **self.auth).json()["results"]
        incoming = [message for message in results if message["sender_slug"] != (self.sender.slug or "")]
        self.assertTrue(incoming)
        for message in incoming:
            self.assertNotEqual(message["sender_name"], self.partner.username)


class TombstoneTests(MessagingBaseTestCase):
    """Content the recipient may no longer see is never served to them."""

    def test_deleted_for_everyone_is_tombstoned_for_the_recipient(self) -> None:
        message = create_direct_message(self.partner, self.sender, "regrettable message")
        from urbanlens.dashboard.services.direct_messages import delete_message_for_everyone

        delete_message_for_everyone(message, self.partner)

        results = self.client.get(self._thread_url(), **self.auth).json()["results"]
        rendered = next(item for item in results if item["id"] == message.pk)
        self.assertIsNotNone(rendered["tombstone"])
        self.assertEqual(rendered["body"], "", "tombstoned content must be blanked, not merely flagged")

    def test_expired_message_is_blanked_for_the_recipient(self) -> None:
        """Retention is enforced server-side, not left to the client."""
        message = create_direct_message(self.partner, self.sender, "burn after reading")
        DirectMessage.objects.filter(pk=message.pk).update(
            sender_delete_after=MessageRetentionChoice.WHEN_READ,
            read_at=timezone.now() - timedelta(minutes=5),
        )

        results = self.client.get(self._thread_url(), **self.auth).json()["results"]
        rendered = next(item for item in results if item["id"] == message.pk)
        self.assertIsNotNone(rendered["tombstone"])
        self.assertEqual(rendered["body"], "")

    def test_the_sender_still_sees_their_own_expired_message(self) -> None:
        """Retention removes the recipient's copy; the sender keeps their own view."""
        message = create_direct_message(self.sender, self.partner, "burn after reading")
        DirectMessage.objects.filter(pk=message.pk).update(
            sender_delete_after=MessageRetentionChoice.WHEN_READ,
            read_at=timezone.now() - timedelta(minutes=5),
        )

        results = self.client.get(self._thread_url(), **self.auth).json()["results"]
        rendered = next(item for item in results if item["id"] == message.pk)
        self.assertIsNone(rendered["tombstone"])
        self.assertEqual(rendered["body"], "burn after reading")


class ReservedSlugRoutingTests(MessagingBaseTestCase):
    """Literal routes are never shadowed by a profile with the same slug."""

    def test_settings_route_resolves_to_the_settings_endpoint(self) -> None:
        Profile.objects.filter(pk=self.partner.pk).update(slug="settings")
        response = self.client.get(reverse("external_api:messages.settings"), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("direct_message_delete_after", response.json())

    def test_conversations_route_resolves_to_the_inbox(self) -> None:
        Profile.objects.filter(pk=self.partner.pk).update(slug="conversations")
        response = self.client.get(reverse("external_api:messages.conversations"), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.json())

    def test_a_reserved_slug_is_refused_as_a_peer(self) -> None:
        """Second line of defense, independent of urlpattern ordering."""
        from urbanlens.dashboard.external_api.views_messaging import _resolve_peer

        Profile.objects.filter(pk=self.partner.pk).update(slug="groups")
        self.assertIsNone(_resolve_peer("groups"))


class PageEnvelopeTests(MessagingBaseTestCase):
    """Both pagination styles emit the envelope their clients expect."""

    def test_conversations_use_page_number_pagination(self) -> None:
        create_direct_message(self.sender, self.partner, "hi")
        payload = self.client.get(reverse("external_api:messages.conversations"), **self.auth).json()
        self.assertEqual(set(payload), {"count", "next", "previous", "results"})
        self.assertIsInstance(payload["count"], int)

    def test_thread_uses_a_cursor_envelope(self) -> None:
        """A live thread has no page count and only walks backwards."""
        create_direct_message(self.sender, self.partner, "hi")
        payload = self.client.get(self._thread_url(), **self.auth).json()
        self.assertEqual(set(payload), {"results", "next", "previous", "count"})
        self.assertIsNone(payload["previous"])
        self.assertIsNone(payload["count"])

    def test_thread_next_link_carries_a_before_cursor(self) -> None:
        for index in range(4):
            create_direct_message(self.sender, self.partner, f"message {index}")
        payload = self.client.get(f"{self._thread_url()}?limit=2", **self.auth).json()

        self.assertEqual(len(payload["results"]), 2)
        self.assertIsNotNone(payload["next"])
        self.assertIn("before=", payload["next"])

    def test_before_cursor_returns_strictly_older_messages(self) -> None:
        for index in range(4):
            create_direct_message(self.sender, self.partner, f"message {index}")
        first = self.client.get(f"{self._thread_url()}?limit=2", **self.auth).json()
        oldest_id = min(item["id"] for item in first["results"])

        older = self.client.get(f"{self._thread_url()}?limit=2&before={oldest_id}", **self.auth).json()
        self.assertTrue(all(item["id"] < oldest_id for item in older["results"]))


class GroupMembershipTests(MessagingBaseTestCase):
    """Only the creator manages membership, enforced server-side."""

    def setUp(self) -> None:
        super().setUp()
        self.third = _profile()
        _open_dms(self.sender, self.partner, self.third)

        from urbanlens.dashboard.services.group_chats import create_group_chat

        self.group = create_group_chat(self.sender, "Crew", [self.partner])
        self.members_url = reverse("external_api:messages.groups.members", kwargs={"group_uuid": self.group.uuid})

    def test_creator_can_add_a_member(self) -> None:
        response = self._post_json(self.members_url, {"member_slugs": [self.third.ensure_slug()]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["added"], 1)

    def test_non_creator_cannot_add_a_member(self) -> None:
        """Client-side assumptions are irrelevant - the service refuses."""
        partner_token = _token_for(self.partner.user)
        response = self.client.post(
            self.members_url,
            data=json.dumps({"member_slugs": [self.third.ensure_slug()]}),
            content_type="application/json",
            **_bearer(partner_token),
        )
        self.assertEqual(response.status_code, 403)

    def test_non_member_gets_404_not_403(self) -> None:
        """A stranger must not be able to confirm the group exists."""
        stranger_token = _token_for(self.third.user)
        response = self.client.get(self.members_url, **_bearer(stranger_token))
        self.assertEqual(response.status_code, 404)

    def test_members_list_reports_the_creator(self) -> None:
        payload = self.client.get(self.members_url, **self.auth).json()
        creators = [member for member in payload if member["is_creator"]]
        self.assertEqual(len(creators), 1)
        self.assertEqual(creators[0]["slug"], self.sender.slug)

    def test_conversation_row_exposes_creator_slug(self) -> None:
        payload = self.client.get(reverse("external_api:messages.conversations"), **self.auth).json()
        groups = [row for row in payload["results"] if row["kind"] == "group"]
        self.assertTrue(groups)
        self.assertEqual(groups[0]["creator_slug"], self.sender.slug)
        self.assertIsNone(groups[0]["peer_slug"])

    def test_dm_rows_have_no_creator_slug(self) -> None:
        create_direct_message(self.sender, self.partner, "hi")
        payload = self.client.get(reverse("external_api:messages.conversations"), **self.auth).json()
        dms = [row for row in payload["results"] if row["kind"] == "dm"]
        self.assertTrue(dms)
        for row in dms:
            self.assertIsNone(row["creator_slug"])
            self.assertIsNone(row["member_count"])


class GroupMessageTests(MessagingBaseTestCase):
    """Sending into a group, including idempotent replay."""

    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.services.group_chats import create_group_chat

        self.group = create_group_chat(self.sender, "Crew", [self.partner])
        self.url = reverse("external_api:messages.groups.messages", kwargs={"group_uuid": self.group.uuid})

    def test_sends_a_group_message(self) -> None:
        response = self._post_json(self.url, {"body": "hello crew"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["body"], "hello crew")

    def test_replayed_client_uuid_returns_the_existing_group_message(self) -> None:
        from urbanlens.dashboard.models.group_chats.model import GroupMessage

        client_uuid = "44444444-4444-4444-8444-444444444444"
        first = self._post_json(self.url, {"body": "once", "client_uuid": client_uuid})
        second = self._post_json(self.url, {"body": "once", "client_uuid": client_uuid})

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(GroupMessage.objects.filter(sender=self.sender, body="once").count(), 1)

    def test_non_member_cannot_send(self) -> None:
        stranger = _profile()
        response = self.client.post(
            self.url,
            data=json.dumps({"body": "let me in"}),
            content_type="application/json",
            **_bearer(_token_for(stranger.user)),
        )
        self.assertEqual(response.status_code, 404)

    def test_image_uuids_is_refused_same_as_image_ids(self) -> None:
        """Attachments aren't supported on group sends - refused with 400, not silently dropped."""
        image = _make_image(self.sender)
        response = self._post_json(self.url, {"body": "photo", "image_uuids": [str(image.uuid)]})
        self.assertEqual(response.status_code, 400)


class RetentionSettingsTests(MessagingBaseTestCase):
    """The retention preference round-trips."""

    def test_get_returns_the_current_setting(self) -> None:
        response = self.client.get(reverse("external_api:messages.settings"), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["direct_message_delete_after"], self.sender.direct_message_delete_after)

    def test_patch_updates_the_setting(self) -> None:
        response = self.client.patch(
            reverse("external_api:messages.settings"),
            data=json.dumps({"direct_message_delete_after": MessageRetentionChoice.WHEN_READ.value}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.sender.refresh_from_db()
        self.assertEqual(self.sender.direct_message_delete_after, MessageRetentionChoice.WHEN_READ.value)

    def test_an_unknown_retention_choice_is_rejected(self) -> None:
        response = self.client.patch(
            reverse("external_api:messages.settings"),
            data=json.dumps({"direct_message_delete_after": "whenever"}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
