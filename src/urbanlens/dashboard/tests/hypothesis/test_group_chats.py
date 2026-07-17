"""Tests for group chats built on the direct message system.

Covers:
- create_group_chat validation, privacy enforcement, and membership rows
- rename/add/remove/leave permission rules (creator manages membership; any
  member renames; anyone leaves)
- The join-time history boundary: members added later cannot see (or fetch)
  messages sent before they joined, including via the older-pages endpoint
- create_group_message validation and read-state behavior
- share_pin_in_group_message creating one PinShare per connected member
- Unread counts and the merged conversation list
- The group HTTP endpoints (thread, send, rename, members, leave, delete)
- The group E2EE key endpoints (member gating, envelope-coverage checks,
  version sequencing, and pre-join envelope invisibility)
- The change-password endpoint (current-password proof, SSO first set,
  bundle rewrap/stale handling)
"""

from __future__ import annotations

import base64
import json
import uuid as uuid_module

from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import AccountKdf
from urbanlens.dashboard.models.e2ee import GroupKey, GroupKeyEnvelope, MessagingKeyBundle
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.group_chats.model import GroupChat, GroupChatMembership, GroupMessage, GroupMessageShare
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.direct_messages import all_conversations_for
from urbanlens.dashboard.services.group_chats import (
    add_group_members,
    create_group_chat,
    create_group_message,
    delete_group_message,
    group_conversations_for,
    group_e2ee_ready,
    group_thread_page,
    remove_group_member,
    rename_group_chat,
    share_pin_in_group_message,
    unread_group_conversation_count,
)


def _profile() -> Profile:
    profile = baker.make("auth.User").profile
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=VisibilityChoice.ANYONE)
    profile.refresh_from_db()
    profile.ensure_slug()
    return profile


def _befriend(a: Profile, b: Profile) -> None:
    Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


def _blob(data: bytes = b"\x01" * 32) -> str:
    return base64.b64encode(data).decode()


def _enroll(profile: Profile) -> MessagingKeyBundle:
    return MessagingKeyBundle.objects.create(
        profile=profile,
        public_key=_blob(),
        recovery_wrapped_secret=_blob(b"\x02" * 72),
    )


class CreateGroupChatTests(TestCase):
    """create_group_chat validates input and enforces per-member privacy."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()

    def test_creates_group_with_memberships(self) -> None:
        group = create_group_chat(self.creator, "Weekend crew", [self.member])
        self.assertEqual(group.creator_id, self.creator.pk)
        self.assertEqual(group.active_memberships().count(), 2)
        self.assertIsNotNone(group.membership_for(self.creator))
        self.assertIsNotNone(group.membership_for(self.member))

    def test_blank_name_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_group_chat(self.creator, "   ", [self.member])

    def test_requires_at_least_one_other_member(self) -> None:
        with self.assertRaises(ValueError):
            create_group_chat(self.creator, "Solo", [self.creator])

    def test_member_privacy_enforced(self) -> None:
        private = _profile()
        Profile.objects.filter(pk=private.pk).update(direct_message_visibility=VisibilityChoice.NO_ONE)
        private.refresh_from_db()
        with self.assertRaises(PermissionError):
            create_group_chat(self.creator, "Nope", [private])

    def test_duplicate_members_collapse(self) -> None:
        group = create_group_chat(self.creator, "Dupes", [self.member, self.member])
        self.assertEqual(group.active_memberships().count(), 2)


class MembershipManagementTests(TestCase):
    """Rename/add/remove/leave follow the documented permission model."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.outsider = _profile()
        self.group = create_group_chat(self.creator, "Crew", [self.member])

    def test_any_member_can_rename(self) -> None:
        rename_group_chat(self.group, self.member, "New name")
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "New name")

    def test_non_member_cannot_rename(self) -> None:
        with self.assertRaises(PermissionError):
            rename_group_chat(self.group, self.outsider, "Hijacked")

    def test_only_creator_adds_members(self) -> None:
        with self.assertRaises(PermissionError):
            add_group_members(self.group, self.member, [self.outsider])
        added = add_group_members(self.group, self.creator, [self.outsider])
        self.assertEqual(len(added), 1)
        self.assertIsNotNone(self.group.membership_for(self.outsider))

    def test_only_creator_removes_others(self) -> None:
        third = _profile()
        add_group_members(self.group, self.creator, [third])
        with self.assertRaises(PermissionError):
            remove_group_member(self.group, self.member, third)
        remove_group_member(self.group, self.creator, third)
        self.assertIsNone(self.group.membership_for(third))

    def test_member_can_leave(self) -> None:
        remove_group_member(self.group, self.member, self.member)
        self.assertIsNone(self.group.membership_for(self.member))
        ended = GroupChatMembership.objects.get(group=self.group, profile=self.member)
        self.assertIsNotNone(ended.left_at)
        self.assertIsNone(ended.removed_by)

    def test_removal_records_remover(self) -> None:
        remove_group_member(self.group, self.creator, self.member)
        ended = GroupChatMembership.objects.get(group=self.group, profile=self.member)
        self.assertEqual(ended.removed_by_id, self.creator.pk)

    def test_readding_creates_new_stint(self) -> None:
        remove_group_member(self.group, self.creator, self.member)
        add_group_members(self.group, self.creator, [self.member])
        self.assertEqual(GroupChatMembership.objects.filter(group=self.group, profile=self.member).count(), 2)


class HistoryVisibilityTests(TestCase):
    """The core guarantee: members never see messages from before they joined."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.group = create_group_chat(self.creator, "History", [self.member])
        self.early = create_group_message(self.creator, self.group, "before the newcomer")

    def test_new_member_cannot_see_prior_messages(self) -> None:
        newcomer = _profile()
        add_group_members(self.group, self.creator, [newcomer])
        membership = self.group.membership_for(newcomer)
        assert membership is not None
        visible = list(GroupMessage.objects.visible_window(membership))
        self.assertEqual(visible, [])

        later = create_group_message(self.creator, self.group, "after the newcomer")
        visible = list(GroupMessage.objects.visible_window(membership))
        self.assertEqual(visible, [later])

    def test_original_members_see_everything(self) -> None:
        membership = self.group.membership_for(self.member)
        assert membership is not None
        self.assertIn(self.early, list(GroupMessage.objects.visible_window(membership)))

    def test_rejoined_member_does_not_see_absence_window(self) -> None:
        remove_group_member(self.group, self.creator, self.member)
        during_absence = create_group_message(self.creator, self.group, "sent while they were gone")
        add_group_members(self.group, self.creator, [self.member])
        membership = self.group.membership_for(self.member)
        assert membership is not None
        visible = list(GroupMessage.objects.visible_window(membership))
        self.assertNotIn(during_absence, visible)
        self.assertNotIn(self.early, visible)

    def test_thread_page_respects_window(self) -> None:
        newcomer = _profile()
        add_group_members(self.group, self.creator, [newcomer])
        membership = self.group.membership_for(newcomer)
        assert membership is not None
        messages, has_more = group_thread_page(membership)
        self.assertEqual(messages, [])
        self.assertFalse(has_more)


class CreateGroupMessageTests(TestCase):
    """create_group_message validates content and updates read state."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.group = create_group_chat(self.creator, "Chat", [self.member])

    def test_non_member_cannot_send(self) -> None:
        outsider = _profile()
        with self.assertRaises(PermissionError):
            create_group_message(outsider, self.group, "hi")

    def test_empty_message_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_group_message(self.creator, self.group, "   ")

    def test_body_and_ciphertext_mutually_exclusive(self) -> None:
        with self.assertRaises(ValueError):
            create_group_message(self.creator, self.group, "hi", ciphertext=_blob(), nonce=_blob(b"\x03" * 24), key_version=1)

    def test_encrypted_message_persists(self) -> None:
        message = create_group_message(self.creator, self.group, "", ciphertext=_blob(), nonce=_blob(b"\x03" * 24), key_version=1)
        self.assertTrue(message.is_encrypted)
        self.assertEqual(message.key_version, 1)

    def test_sender_read_state_advances(self) -> None:
        create_group_message(self.creator, self.group, "hello")
        sender_membership = self.group.membership_for(self.creator)
        assert sender_membership is not None
        self.assertEqual(GroupMessage.objects.unread_for(sender_membership).count(), 0)
        member_membership = self.group.membership_for(self.member)
        assert member_membership is not None
        self.assertEqual(GroupMessage.objects.unread_for(member_membership).count(), 1)

    def test_unread_group_conversation_count(self) -> None:
        self.assertEqual(unread_group_conversation_count(self.member), 0)
        create_group_message(self.creator, self.group, "hello")
        self.assertEqual(unread_group_conversation_count(self.member), 1)

    def test_delete_tombstones_for_others(self) -> None:
        message = create_group_message(self.creator, self.group, "oops")
        with self.assertRaises(PermissionError):
            delete_group_message(message, self.member)
        delete_group_message(message, self.creator)
        message.refresh_from_db()
        self.assertIsNotNone(message.deleted_at)
        self.assertEqual(message.tombstone_text_for(self.member.pk), "Message deleted")
        self.assertIsNone(message.tombstone_text_for(self.creator.pk))


class GroupPinShareTests(TestCase):
    """Sharing a pin into a group creates one PinShare per connected member."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.friend_a = _profile()
        self.friend_b = _profile()
        self.stranger = _profile()
        _befriend(self.creator, self.friend_a)
        _befriend(self.creator, self.friend_b)
        self.group = create_group_chat(self.creator, "Explorers", [self.friend_a, self.friend_b, self.stranger])
        self.pin = baker.make("dashboard.Pin", profile=self.creator)

    def test_share_creates_pin_share_per_connected_member(self) -> None:
        message = share_pin_in_group_message(self.creator, self.group, self.pin, "check this out")
        recipients = set(GroupMessageShare.objects.filter(message=message).values_list("recipient_id", flat=True))
        self.assertEqual(recipients, {self.friend_a.pk, self.friend_b.pk})
        self.assertEqual(PinShare.objects.filter(pin=self.pin, from_profile=self.creator).count(), 2)
        shared_to = set(PinShare.objects.filter(pin=self.pin).values_list("to_profile_id", flat=True))
        self.assertEqual(shared_to, {self.friend_a.pk, self.friend_b.pk})

    def test_share_for_returns_own_row_only(self) -> None:
        message = share_pin_in_group_message(self.creator, self.group, self.pin, "look")
        message = GroupMessage.objects.prefetch_related("shares").get(pk=message.pk)
        own = message.share_for(self.friend_a.pk)
        self.assertIsNotNone(own)
        self.assertEqual(own.recipient_id, self.friend_a.pk)
        self.assertIsNone(message.share_for(self.stranger.pk))


class ConversationMergeTests(TestCase):
    """all_conversations_for interleaves 1:1 and group rows, newest first."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.friend = _profile()

    def test_group_rows_included(self) -> None:
        group = create_group_chat(self.me, "Mixed", [self.friend])
        create_group_message(self.me, group, "hello group")
        conversations = all_conversations_for(self.friend)
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["kind"], "group")
        self.assertEqual(conversations[0]["group"].pk, group.pk)
        self.assertEqual(conversations[0]["unread_count"], 1)

    def test_group_without_messages_still_listed(self) -> None:
        create_group_chat(self.me, "Quiet", [self.friend])
        rows = group_conversations_for(self.friend)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["last_message"])


class GroupEndpointTests(TestCase):
    """The group HTTP endpoints enforce membership and drive the thread."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.outsider = _profile()
        self.group = create_group_chat(self.creator, "Endpoint crew", [self.member])
        self.client.force_login(self.creator.user)

    def _url(self, name: str, **kwargs) -> str:
        return reverse(name, kwargs={"group_uuid": self.group.uuid, **kwargs})

    def test_create_endpoint(self) -> None:
        other = _profile()
        response = self.client.post(reverse("messages.group.create"), {"name": "Fresh", "member_slugs": [other.slug]})
        self.assertEqual(response.status_code, 201)
        payload = json.loads(response.content)
        self.assertTrue(GroupChat.objects.filter(uuid=payload["uuid"]).exists())

    def test_create_endpoint_rejects_empty(self) -> None:
        response = self.client.post(reverse("messages.group.create"), {"name": "Nobody"})
        self.assertEqual(response.status_code, 400)

    def test_thread_view_for_member(self) -> None:
        create_group_message(self.member, self.group, "hi there")
        response = self.client.get(self._url("messages.group"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "hi there")

    def test_thread_view_404_for_non_member(self) -> None:
        self.client.force_login(self.outsider.user)
        response = self.client.get(self._url("messages.group"))
        self.assertEqual(response.status_code, 404)

    def test_thread_view_404_after_leaving(self) -> None:
        remove_group_member(self.group, self.member, self.member)
        self.client.force_login(self.member.user)
        response = self.client.get(self._url("messages.group"))
        self.assertEqual(response.status_code, 404)

    def test_send_fallback(self) -> None:
        response = self.client.post(self._url("messages.group.send"), {"body": "posted via fallback"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(GroupMessage.objects.filter(group=self.group, body="posted via fallback").exists())

    def test_rename_endpoint(self) -> None:
        response = self.client.post(self._url("messages.group.rename"), {"name": "Renamed"})
        self.assertEqual(response.status_code, 200)
        self.group.refresh_from_db()
        self.assertEqual(self.group.name, "Renamed")

    def test_remove_member_endpoint(self) -> None:
        response = self.client.post(self._url("messages.group.members.remove"), {"profile_slug": self.member.slug})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.group.membership_for(self.member))

    def test_leave_endpoint(self) -> None:
        self.client.force_login(self.member.user)
        response = self.client.post(self._url("messages.group.leave"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.group.membership_for(self.member))

    def test_older_endpoint_respects_join_boundary(self) -> None:
        create_group_message(self.creator, self.group, "ancient history")
        newcomer = _profile()
        add_group_members(self.group, self.creator, [newcomer])
        marker = create_group_message(self.creator, self.group, "visible to newcomer")
        self.client.force_login(newcomer.user)
        response = self.client.get(self._url("messages.group.older"), {"before": marker.pk})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "ancient history")


class GroupKeyEndpointTests(TestCase):
    """The group E2EE key endpoints gate on membership and envelope coverage."""

    def setUp(self) -> None:
        super().setUp()
        self.creator = _profile()
        self.member = _profile()
        self.group = create_group_chat(self.creator, "Locked", [self.member])
        self.url = reverse("e2ee.group_key", kwargs={"group_uuid": self.group.uuid})
        self.client.force_login(self.creator.user)

    def _post(self, payload: dict) -> object:
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_non_member_404(self) -> None:
        outsider = _profile()
        self.client.force_login(outsider.user)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_get_reports_members_only_when_all_enrolled(self) -> None:
        _enroll(self.creator)
        response = self.client.get(self.url)
        payload = json.loads(response.content)
        self.assertIsNone(payload["members"])
        self.assertTrue(payload["needs_rotation"])
        self.assertFalse(group_e2ee_ready(self.group))

        _enroll(self.member)
        payload = json.loads(self.client.get(self.url).content)
        self.assertEqual({m["slug"] for m in payload["members"]}, {self.creator.slug, self.member.slug})
        self.assertTrue(group_e2ee_ready(self.group))

    def test_post_requires_exact_member_coverage(self) -> None:
        _enroll(self.creator)
        _enroll(self.member)
        response = self._post({"version": 1, "wrapped": {self.creator.slug: _blob()}})
        self.assertEqual(response.status_code, 409)

    def test_post_creates_version_and_get_returns_own_envelope(self) -> None:
        _enroll(self.creator)
        _enroll(self.member)
        wrapped = {self.creator.slug: _blob(b"\x0a" * 48), self.member.slug: _blob(b"\x0b" * 48)}
        response = self._post({"version": 1, "wrapped": wrapped})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(GroupKey.objects.filter(group=self.group).count(), 1)
        self.assertEqual(GroupKeyEnvelope.objects.filter(key__group=self.group).count(), 2)

        payload = json.loads(self.client.get(self.url).content)
        self.assertEqual(payload["latest"], 1)
        self.assertFalse(payload["needs_rotation"])
        self.assertEqual(len(payload["keys"]), 1)
        self.assertEqual(payload["keys"][0]["wrapped_key"], wrapped[self.creator.slug])

    def test_post_wrong_version_409(self) -> None:
        _enroll(self.creator)
        _enroll(self.member)
        wrapped = {self.creator.slug: _blob(), self.member.slug: _blob()}
        self.assertEqual(self._post({"version": 5, "wrapped": wrapped}).status_code, 409)

    def test_membership_change_flags_rotation_and_hides_prior_envelopes(self) -> None:
        _enroll(self.creator)
        _enroll(self.member)
        wrapped = {self.creator.slug: _blob(), self.member.slug: _blob()}
        self._post({"version": 1, "wrapped": wrapped})

        newcomer = _profile()
        _enroll(newcomer)
        add_group_members(self.group, self.creator, [newcomer])

        payload = json.loads(self.client.get(self.url).content)
        self.assertTrue(payload["needs_rotation"])

        # The newcomer holds no envelope for version 1 - pre-join ciphertext
        # stays cryptographically out of reach.
        self.client.force_login(newcomer.user)
        newcomer_payload = json.loads(self.client.get(self.url).content)
        self.assertEqual(newcomer_payload["keys"], [])
        self.assertEqual(newcomer_payload["latest"], 1)


class ChangePasswordEndpointTests(TestCase):
    """The change-password endpoint verifies possession and reconciles E2EE state."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        self.user = self.profile.user
        self.url = reverse("e2ee.change_password")

    def _post(self, payload: dict) -> object:
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_wrong_current_password_403(self) -> None:
        self.user.set_password("old-password")
        self.user.save()
        self.client.force_login(self.user)
        response = self._post({"current_secret": "not-it", "new_auth_key": _blob(), "new_auth_salt": _blob(b"\x04" * 16)})
        self.assertEqual(response.status_code, 403)

    def test_change_rotates_credential_and_kdf(self) -> None:
        self.user.set_password("old-password")
        self.user.save()
        self.client.force_login(self.user)
        new_key = _blob(b"\x05" * 32)
        salt = _blob(b"\x06" * 16)
        response = self._post({"current_secret": "old-password", "new_auth_key": new_key, "new_auth_salt": salt})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(new_key))
        self.assertEqual(AccountKdf.objects.get(user=self.user).auth_salt, salt)

    def test_sso_account_sets_password_without_current(self) -> None:
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
        new_key = _blob(b"\x07" * 32)
        response = self._post({"new_auth_key": new_key, "new_auth_salt": _blob(b"\x08" * 16)})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.has_usable_password())
        self.assertFalse(json.loads(response.content)["had_password"])

    def test_missing_wrap_marks_bundle_stale(self) -> None:
        self.user.set_password("old-password")
        self.user.save()
        bundle = _enroll(self.profile)
        MessagingKeyBundle.objects.filter(pk=bundle.pk).update(password_wrapped_secret=_blob(), password_wrap_salt=_blob(b"\x09" * 16))
        self.client.force_login(self.user)
        response = self._post({"current_secret": "old-password", "new_auth_key": _blob(), "new_auth_salt": _blob(b"\x04" * 16)})
        self.assertEqual(response.status_code, 200)
        bundle.refresh_from_db()
        self.assertTrue(bundle.password_wrap_stale)

    def test_provided_wrap_replaces_and_clears_stale(self) -> None:
        self.user.set_password("old-password")
        self.user.save()
        bundle = _enroll(self.profile)
        MessagingKeyBundle.objects.filter(pk=bundle.pk).update(password_wrap_stale=True)
        self.client.force_login(self.user)
        new_wrap = _blob(b"\x0c" * 72)
        wrap_salt = _blob(b"\x0d" * 16)
        response = self._post(
            {
                "current_secret": "old-password",
                "new_auth_key": _blob(),
                "new_auth_salt": _blob(b"\x04" * 16),
                "password_wrapped_secret": new_wrap,
                "password_wrap_salt": wrap_salt,
            },
        )
        self.assertEqual(response.status_code, 200)
        bundle.refresh_from_db()
        self.assertEqual(bundle.password_wrapped_secret, new_wrap)
        self.assertFalse(bundle.password_wrap_stale)


class SetPasswordPromptTests(TestCase):
    """Passwordless accounts are routed to the set-password prompt after login."""

    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()
        self.user = self.profile.user
        # The very first user in a fresh test database is auto-promoted to
        # site admin, whose post-login destination is /setup/ - patch that
        # branch away so these tests exercise the password prompt itself.
        from unittest import mock

        patcher = mock.patch("urbanlens.dashboard.controllers.account.should_redirect_to_site_admin", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_post_login_redirects_passwordless_user(self) -> None:
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        self.user.set_unusable_password()
        self.user.save()
        ProfileModel.objects.filter(pk=self.profile.pk).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(self.user)
        response = self.client.get(reverse("post_login"))
        self.assertRedirects(response, reverse("account.set_password"), fetch_redirect_response=False)

    def test_skip_suppresses_prompt_for_session(self) -> None:
        from urbanlens.dashboard.models.profile.model import Profile as ProfileModel

        self.user.set_unusable_password()
        self.user.save()
        ProfileModel.objects.filter(pk=self.profile.pk).update(welcome_onboarding_complete=True, profile_setup_complete=True)
        self.client.force_login(self.user)
        self.client.get(reverse("account.set_password.skip"))
        response = self.client.get(reverse("post_login"))
        self.assertNotEqual(response.headers.get("Location"), reverse("account.set_password"))

    def test_prompt_redirects_users_with_password(self) -> None:
        self.user.set_password("some-password")
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse("account.set_password"))
        self.assertRedirects(response, reverse("post_login"), fetch_redirect_response=False)

    def test_prompt_renders_for_passwordless_user(self) -> None:
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse("account.set_password"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Set a password")
        self.assertContains(response, reverse("account.set_password.skip"))


class SettingsPasswordSectionTests(TestCase):
    """The settings Security tab offers change-password (or first-time set)."""

    def test_change_password_form_for_password_account(self) -> None:
        profile = _profile()
        profile.user.set_password("hunter22")
        profile.user.save()
        self.client.force_login(profile.user)
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current password")
        self.assertContains(response, reverse("password_reset"))

    def test_set_password_form_for_sso_account(self) -> None:
        profile = _profile()
        profile.user.set_unusable_password()
        profile.user.save()
        self.client.force_login(profile.user)
        response = self.client.get(reverse("settings.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Set password")
        self.assertNotContains(response, "Current password")


class GroupUuidRoutingTests(TestCase):
    """Unknown group uuids 404 cleanly rather than leaking existence."""

    def test_random_uuid_404(self) -> None:
        profile = _profile()
        self.client.force_login(profile.user)
        response = self.client.get(reverse("messages.group", kwargs={"group_uuid": uuid_module.uuid4()}))
        self.assertEqual(response.status_code, 404)
