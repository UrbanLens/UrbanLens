"""Tests for direct messages between users.

Covers:
- Profile.direct_message_visibility default and PrivacySettingsForm persistence
- Profile.accepts_direct_messages_from / services.can_direct_message for each
  VisibilityChoice, including the reply exception and community gating
- create_direct_message validation, permission enforcement, and notifications
- DirectMessageQuerySet conversation helpers (between/unread_for/conversation_rows)
- conversations_for / has_used_direct_messages service helpers
- The HTTP endpoints (page, conversation, send, dropdown, unread count)
"""

from __future__ import annotations

from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.forms.settings_form import PrivacySettingsForm
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.direct_messages import (
    REACTION_PICKER_EMOJIS,
    can_direct_message,
    conversations_for,
    create_direct_message,
    has_used_direct_messages,
    is_safe_reaction_emoji,
)
from urbanlens.dashboard.services.text_limits import MAX_DIRECT_MESSAGE_LENGTH

_db_settings = settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_visibility_choices = st.sampled_from(list(VisibilityChoice.values))


def _profile() -> Profile:
    return baker.make("auth.User").profile


def _make_accepted_friendship(a: Profile, b: Profile) -> Friendship:
    return Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


def _set_dm_visibility(profile: Profile, visibility: str) -> None:
    Profile.objects.filter(pk=profile.pk).update(direct_message_visibility=visibility)
    profile.refresh_from_db()


# -- Setting default and form persistence ---------------------------------------


class DirectMessageVisibilityDefaultTests(TestCase):
    """The new privacy field defaults to ANYTHING_IN_COMMON like its siblings."""

    def test_default_is_anything_in_common(self) -> None:
        self.assertEqual(_profile().direct_message_visibility, VisibilityChoice.ANYTHING_IN_COMMON)

    def test_privacy_form_includes_field(self) -> None:
        self.assertIn("direct_message_visibility", PrivacySettingsForm(instance=_profile()).fields)

    def test_privacy_form_persists_choice(self) -> None:
        profile = _profile()
        data = {
            "profile_visibility": VisibilityChoice.ANYONE,
            "comment_visibility": VisibilityChoice.ANYONE,
            "friend_request_visibility": VisibilityChoice.ANYONE,
            "photo_upload_visibility": VisibilityChoice.ANYONE,
            "viewer_photo_filter": VisibilityChoice.ANYONE,
            "trip_pin_location_visibility": VisibilityChoice.ANYONE,
            "contact_visibility": VisibilityChoice.ANYONE,
            "direct_message_visibility": VisibilityChoice.FRIENDS,
        }
        form = PrivacySettingsForm(data=data, instance=profile)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        profile.refresh_from_db()
        self.assertEqual(profile.direct_message_visibility, VisibilityChoice.FRIENDS)


# -- Permission evaluation --------------------------------------------------------


class CanDirectMessageTests(TestCase):
    """can_direct_message honors the recipient's setting, community gating, and replies."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()

    def test_cannot_message_self(self) -> None:
        _set_dm_visibility(self.sender, VisibilityChoice.ANYONE)
        self.assertFalse(can_direct_message(self.sender, self.sender))

    def test_anyone_permits_stranger(self) -> None:
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        self.assertTrue(can_direct_message(self.sender, self.recipient))

    def test_no_one_blocks_stranger(self) -> None:
        _set_dm_visibility(self.recipient, VisibilityChoice.NO_ONE)
        self.assertFalse(can_direct_message(self.sender, self.recipient))

    def test_no_one_blocks_friend(self) -> None:
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.NO_ONE)
        self.assertFalse(can_direct_message(self.sender, self.recipient))

    def test_friends_permits_friend(self) -> None:
        _make_accepted_friendship(self.sender, self.recipient)
        _set_dm_visibility(self.recipient, VisibilityChoice.FRIENDS)
        self.assertTrue(can_direct_message(self.sender, self.recipient))

    def test_friends_blocks_stranger(self) -> None:
        _set_dm_visibility(self.recipient, VisibilityChoice.FRIENDS)
        self.assertFalse(can_direct_message(self.sender, self.recipient))

    def test_reply_exception_overrides_no_one(self) -> None:
        """Once the recipient has messaged the sender, the sender may always reply."""
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        _set_dm_visibility(self.sender, VisibilityChoice.ANYONE)
        DirectMessage.objects.create(sender=self.recipient, recipient=self.sender, body="hi")
        _set_dm_visibility(self.recipient, VisibilityChoice.NO_ONE)
        self.assertTrue(can_direct_message(self.sender, self.recipient))

    def test_community_disabled_recipient_blocks(self) -> None:
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        Profile.objects.filter(pk=self.recipient.pk).update(community_enabled=False)
        self.recipient.refresh_from_db()
        self.assertFalse(can_direct_message(self.sender, self.recipient))

    def test_community_disabled_sender_blocks(self) -> None:
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)
        Profile.objects.filter(pk=self.sender.pk).update(community_enabled=False)
        self.sender.refresh_from_db()
        self.assertFalse(can_direct_message(self.sender, self.recipient))


class FriendsAlwaysQualifyPropertyTests(TestCase):
    """Accepted friends qualify for every visibility option except NO_ONE."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _make_accepted_friendship(self.sender, self.recipient)

    @_db_settings
    @given(visibility=_visibility_choices)
    def test_friend_can_message_unless_no_one(self, visibility: str) -> None:
        _set_dm_visibility(self.recipient, visibility)
        expected = visibility != VisibilityChoice.NO_ONE
        self.assertEqual(can_direct_message(self.sender, self.recipient), expected)


# -- create_direct_message -------------------------------------------------------


class CreateDirectMessageTests(TestCase):
    """Validation, permission enforcement, and notification behavior."""

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    def test_creates_and_strips_message(self) -> None:
        message = create_direct_message(self.sender, self.recipient, "  hello there  ")
        self.assertEqual(message.body, "hello there")
        self.assertEqual(message.sender, self.sender)
        self.assertEqual(message.recipient, self.recipient)
        self.assertTrue(message.is_unread)

    def test_blank_body_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            create_direct_message(self.sender, self.recipient, "   ")

    def test_too_long_body_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            create_direct_message(self.sender, self.recipient, "x" * (MAX_DIRECT_MESSAGE_LENGTH + 1))

    def test_blocked_sender_raises_permission_error(self) -> None:
        _set_dm_visibility(self.recipient, VisibilityChoice.NO_ONE)
        with self.assertRaises(PermissionError):
            create_direct_message(self.sender, self.recipient, "hello")
        self.assertFalse(DirectMessage.objects.exists())

    def test_first_message_notifies_recipient(self) -> None:
        create_direct_message(self.sender, self.recipient, "hello")
        notification = NotificationLog.objects.get(profile=self.recipient)
        self.assertEqual(notification.notification_type, NotificationType.MESSAGE)
        self.assertEqual(notification.source_profile, self.sender)

    def test_second_unread_message_does_not_renotify(self) -> None:
        create_direct_message(self.sender, self.recipient, "hello")
        create_direct_message(self.sender, self.recipient, "you there?")
        self.assertEqual(NotificationLog.objects.filter(profile=self.recipient).count(), 1)

    def test_message_after_read_notifies_again(self) -> None:
        create_direct_message(self.sender, self.recipient, "hello")
        DirectMessage.objects.unread_for(self.recipient).mark_read()
        create_direct_message(self.sender, self.recipient, "again")
        self.assertEqual(NotificationLog.objects.filter(profile=self.recipient).count(), 2)

    def test_notification_pref_none_suppresses_notification(self) -> None:
        NotificationPreference.objects.create(profile=self.recipient, message="none")
        create_direct_message(self.sender, self.recipient, "hello")
        self.assertFalse(NotificationLog.objects.filter(profile=self.recipient).exists())
        self.assertTrue(DirectMessage.objects.exists())

    def test_map_only_message_notification_has_nonblank_preview(self) -> None:
        markup_map = MarkupMap.objects.create(profile=self.sender)
        create_direct_message(self.sender, self.recipient, "", markup_map_uuid=str(markup_map.uuid))
        notification = NotificationLog.objects.get(profile=self.recipient)
        self.assertIn("map", notification.message.lower())


# -- QuerySet helpers --------------------------------------------------------------


class DirectMessageQuerySetTests(TestCase):
    """between/unread_for/mark_read/conversation_rows behave as documented."""

    def setUp(self) -> None:
        super().setUp()
        self.alice = _profile()
        self.bob = _profile()
        self.carol = _profile()

    def _msg(self, sender: Profile, recipient: Profile, body: str = "hi") -> DirectMessage:
        return DirectMessage.objects.create(sender=sender, recipient=recipient, body=body)

    def test_between_is_symmetric(self) -> None:
        first = self._msg(self.alice, self.bob)
        second = self._msg(self.bob, self.alice)
        self._msg(self.alice, self.carol)
        self.assertEqual(list(DirectMessage.objects.between(self.alice, self.bob)), [first, second])
        self.assertEqual(list(DirectMessage.objects.between(self.bob, self.alice)), [first, second])

    def test_unread_for_and_mark_read(self) -> None:
        self._msg(self.alice, self.bob)
        self._msg(self.alice, self.bob)
        self._msg(self.bob, self.alice)
        self.assertEqual(DirectMessage.objects.unread_for(self.bob).count(), 2)
        updated = DirectMessage.objects.between(self.alice, self.bob).filter(recipient=self.bob).mark_read()
        self.assertEqual(updated, 2)
        self.assertEqual(DirectMessage.objects.unread_for(self.bob).count(), 0)
        self.assertEqual(DirectMessage.objects.unread_for(self.alice).count(), 1)

    def test_conversation_rows_groups_by_partner(self) -> None:
        self._msg(self.alice, self.bob, "to bob")
        last_bob = self._msg(self.bob, self.alice, "from bob")
        last_carol = self._msg(self.carol, self.alice, "from carol")

        rows = list(DirectMessage.objects.conversation_rows(self.alice))
        self.assertEqual(len(rows), 2)
        # Most recently active first.
        self.assertEqual(rows[0]["partner_id"], self.carol.pk)
        self.assertEqual(rows[0]["last_message_id"], last_carol.pk)
        self.assertEqual(rows[0]["unread_count"], 1)
        self.assertEqual(rows[1]["partner_id"], self.bob.pk)
        self.assertEqual(rows[1]["last_message_id"], last_bob.pk)
        self.assertEqual(rows[1]["unread_count"], 1)

    def test_conversations_for_returns_partner_objects(self) -> None:
        self._msg(self.alice, self.bob, "to bob")
        conversations = conversations_for(self.alice)
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["partner"], self.bob)
        self.assertEqual(conversations[0]["last_message"].body, "to bob")
        self.assertEqual(conversations[0]["unread_count"], 0)

    def test_has_used_direct_messages(self) -> None:
        self.assertFalse(has_used_direct_messages(self.alice))
        self._msg(self.alice, self.bob)
        self.assertTrue(has_used_direct_messages(self.alice))
        self.assertTrue(has_used_direct_messages(self.bob))
        self.assertFalse(has_used_direct_messages(self.carol))


# -- HTTP endpoints ----------------------------------------------------------------


class ReactionEmojiValidationTests(TestCase):
    """`is_safe_reaction_emoji` accepts genuine emoji, rejects render-unsafe input."""

    def test_picker_emojis_are_all_accepted(self) -> None:
        for emoji in REACTION_PICKER_EMOJIS:
            self.assertTrue(is_safe_reaction_emoji(emoji), emoji)

    def test_empty_is_rejected(self) -> None:
        self.assertFalse(is_safe_reaction_emoji(""))

    @given(payload=st.text(alphabet="<>&\"'`=/\\{}abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=10))
    @_db_settings
    def test_any_html_or_alpha_character_is_rejected(self, payload: str) -> None:
        # Every character in the strategy's alphabet is forbidden, so any
        # non-empty string drawn from it must be rejected outright.
        self.assertFalse(is_safe_reaction_emoji(payload))


class DirectMessageEndpointTests(TestCase):
    """Auth, privacy enforcement, and read-marking on the messages endpoints."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)
        self.me.ensure_slug()
        self.partner.ensure_slug()
        self.client.force_login(self.me.user)

    def test_messages_page_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("messages.view"))
        self.assertEqual(response.status_code, 302)

    def test_messages_page_renders(self) -> None:
        response = self.client.get(reverse("messages.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Messages")

    def test_send_creates_message(self) -> None:
        response = self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}),
            {"body": "hello"},
        )
        self.assertEqual(response.status_code, 200)
        message = DirectMessage.objects.get()
        self.assertEqual(message.sender, self.me)
        self.assertEqual(message.recipient, self.partner)

    def test_send_blocked_returns_403(self) -> None:
        _set_dm_visibility(self.partner, VisibilityChoice.NO_ONE)
        response = self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}),
            {"body": "hello"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DirectMessage.objects.exists())

    def test_send_blank_returns_400(self) -> None:
        response = self.client.post(
            reverse("messages.send", kwargs={"profile_slug": self.partner.slug}),
            {"body": "   "},
        )
        self.assertEqual(response.status_code, 400)

    def test_conversation_marks_messages_read(self) -> None:
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")
        self.assertEqual(DirectMessage.objects.unread_for(self.me).count(), 1)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DirectMessage.objects.unread_for(self.me).count(), 0)

    def test_conversation_with_hidden_stranger_404s(self) -> None:
        """No existing conversation + privacy rejects sending = the URL doesn't resolve."""
        _set_dm_visibility(self.partner, VisibilityChoice.NO_ONE)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}))
        self.assertEqual(response.status_code, 404)

    def test_conversation_with_own_slug_404s(self) -> None:
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.me.slug}))
        self.assertEqual(response.status_code, 404)

    def test_unread_count_badge(self) -> None:
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")
        response = self.client.get(reverse("messages.unread_count"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1")

    def test_dropdown_lists_conversation(self) -> None:
        # A DM conversation alone grants no profile-view standing, so make the
        # partner's profile visible - otherwise the row shows "Former contact".
        Profile.objects.filter(pk=self.partner.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="dropdown preview text")
        response = self.client.get(reverse("messages.dropdown"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.partner.username)
        self.assertContains(response, "dropdown preview")

    def test_dropdown_hides_read_conversations(self) -> None:
        """A conversation disappears from the dropdown once its messages are read."""
        message = DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="dropdown preview text")
        DirectMessage.objects.filter(pk=message.pk).mark_read()
        response = self.client.get(reverse("messages.dropdown"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "dropdown preview")
        self.assertContains(response, "caught up")

    def test_dropdown_empty_state_without_any_history(self) -> None:
        response = self.client.get(reverse("messages.dropdown"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No messages yet")

    def test_recipient_search_excludes_blocked(self) -> None:
        _set_dm_visibility(self.partner, VisibilityChoice.NO_ONE)
        response = self.client.get(reverse("messages.recipients"), {"q": self.partner.username[:5]})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f">{self.partner.username}<")

    def test_navbar_icon_hidden_until_first_message(self) -> None:
        response = self.client.get(reverse("messages.view"))
        self.assertNotContains(response, 'id="nav-msg"')
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")
        response = self.client.get(reverse("messages.view"))
        self.assertContains(response, 'id="nav-msg"')

    def test_react_with_emoji_succeeds(self) -> None:
        message = DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")
        response = self.client.post(
            reverse("messages.react", kwargs={"profile_slug": self.partner.slug, "message_id": message.pk}),
            {"emoji": "🔥"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(message.reactions.filter(emoji="🔥").exists())

    def test_react_with_html_payload_rejected(self) -> None:
        """A reaction is broadcast to and rendered by the other party, so an
        emoji carrying markup/JS must be refused before it is ever stored."""
        message = DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")
        response = self.client.post(
            reverse("messages.react", kwargs={"profile_slug": self.partner.slug, "message_id": message.pk}),
            {"emoji": '<img src>'},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(message.reactions.exists())
