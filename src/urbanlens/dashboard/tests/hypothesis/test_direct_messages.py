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

from unittest.mock import patch

from django.urls import reverse
from hypothesis import HealthCheck, given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
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
    thread_page,
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
            "common_pins_visibility": VisibilityChoice.ANYONE,
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


class ReactionEmojiValidationTests(SimpleTestCase):
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

    def test_unread_count_label(self) -> None:
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="hi")
        response = self.client.get(reverse("messages.unread_count"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1")

    def test_encrypted_message_placeholder_has_no_redundant_lock_emoji(self) -> None:
        """Regression guard: the server-rendered "Decrypting…" placeholder used
        to start with a 🔒 emoji, duplicating the separate dedicated lock icon
        (.dm-lock-icon, "End-to-end encrypted") already rendered right next to
        it - two lock glyphs for one encrypted message."""
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, ciphertext="abc123", nonce="def456", key_version=1)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}))
        content = response.content.decode()
        self.assertIn("Decrypting…", content)
        self.assertNotIn("🔒 Decrypting", content)
        self.assertIn("dm-lock-icon", content)

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

    def test_recipient_search_finds_a_visible_messageable_profile(self) -> None:
        Profile.objects.filter(pk=self.partner.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        response = self.client.get(reverse("messages.recipients"), {"q": self.partner.username[:5]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.partner.username)

    def test_recipient_search_excludes_a_profile_masked_from_the_requester(self) -> None:
        """Regression: a messageable profile with profile_visibility=NO_ONE was
        still returned with its real username/slug/avatar, letting anyone
        enumerate hidden identities by substring even though every other
        surface (thread, sidebar, notifications) masks them."""
        Profile.objects.filter(pk=self.partner.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        response = self.client.get(reverse("messages.recipients"), {"q": self.partner.username[:5]})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.partner.username)

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
            {"emoji": "<img src>"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(message.reactions.exists())


class ConversationPaginationTests(TestCase):
    """Long conversations load a bounded page of history, not the entire thread."""

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)
        self.me.ensure_slug()
        self.partner.ensure_slug()
        self.client.force_login(self.me.user)

    def _create_messages(self, count: int) -> list[DirectMessage]:
        return [DirectMessage.objects.create(sender=self.me, recipient=self.partner, body=f"msg {i}") for i in range(count)]

    def test_thread_page_caps_at_limit_and_flags_more(self) -> None:
        messages = self._create_messages(5)
        page, has_more_older = thread_page(self.me, self.partner, limit=3)
        self.assertEqual([m.pk for m in page], [m.pk for m in messages[-3:]])
        self.assertTrue(has_more_older)

    def test_thread_page_reports_no_more_when_everything_fits(self) -> None:
        messages = self._create_messages(3)
        page, has_more_older = thread_page(self.me, self.partner, limit=10)
        self.assertEqual([m.pk for m in page], [m.pk for m in messages])
        self.assertFalse(has_more_older)

    def test_thread_page_before_id_paginates_backwards(self) -> None:
        messages = self._create_messages(5)
        newest_page, _ = thread_page(self.me, self.partner, limit=2)
        self.assertEqual([m.pk for m in newest_page], [m.pk for m in messages[-2:]])
        older_page, has_more_older = thread_page(self.me, self.partner, before_id=newest_page[0].pk, limit=2)
        self.assertEqual([m.pk for m in older_page], [m.pk for m in messages[1:3]])
        self.assertTrue(has_more_older)

    def test_conversation_view_only_renders_latest_page(self) -> None:
        self._create_messages(60)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "msg 0<")
        self.assertContains(response, "msg 59<")
        self.assertContains(response, 'id="dm-load-older-sentinel"')

    def test_conversation_view_omits_sentinel_when_history_fits(self) -> None:
        self._create_messages(5)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}))
        self.assertEqual(response.status_code, 200)
        # The identifier alone also appears in the page's inline JS (which checks
        # `elt.id` regardless of whether the sentinel element is ever rendered),
        # so assert against the element's actual markup, not the bare id string.
        self.assertNotContains(response, 'id="dm-load-older-sentinel"')

    def test_older_messages_endpoint_returns_earlier_page(self) -> None:
        self._create_messages(60)
        first_page, _ = thread_page(self.me, self.partner)
        response = self.client.get(
            reverse("messages.older", kwargs={"profile_slug": self.partner.slug}),
            {"before": first_page[0].pk},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "msg 9<")
        self.assertNotContains(response, "msg 10<")
        self.assertNotContains(response, "msg 59<")

    def test_older_messages_endpoint_requires_valid_before(self) -> None:
        response = self.client.get(reverse("messages.older", kwargs={"profile_slug": self.partner.slug}), {"before": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_older_messages_endpoint_requires_login(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("messages.older", kwargs={"profile_slug": self.partner.slug}), {"before": "1"})
        self.assertEqual(response.status_code, 302)


class ThreadRenderingRegressionTests(TestCase):
    """Regression coverage for a reported "no longer see X" batch of complaints:
    per-message controls, timestamps, and date-separator headers between days.

    Investigated first: the hover-reveal CSS for .dm-bubble__menu-btn/.dm-bubble__time
    (opacity: 0 until hover/focus/tap) predates every change in this session by over a
    week, and the group-chat commit touched neither _message_items.html nor this CSS -
    so there was no code regression to find for the "controls"/"timestamps" complaints;
    that's long-standing, deliberate chat-app-style design, not a bug. This class instead
    locks in the one thing that actually deserved direct verification: the date-separator
    <ifchanged> logic on the server, which had zero prior test coverage - plus confirms,
    directly against the rendered markup, that every control/timestamp element these
    complaints named is genuinely present (not literally missing), so nothing to disable/
    hover is disabled by nothing being there at all.
    """

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)
        self.me.ensure_slug()
        self.partner.ensure_slug()
        self.client.force_login(self.me.user)

    def _render(self) -> str:
        # HTMX-partial request: just _thread.html's own markup, not the full
        # page (which also embeds these same class names as JS selector
        # strings in its <script> block - counting those too would inflate
        # every assertion below and hide a real markup regression).
        response = self.client.get(
            reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_date_separator_appears_once_per_distinct_day(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        base = timezone.now() - timedelta(days=2)
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="day one - a")
        DirectMessage.objects.filter(body="day one - a").update(created=base)
        DirectMessage.objects.create(sender=self.me, recipient=self.partner, body="day one - b")
        DirectMessage.objects.filter(body="day one - b").update(created=base + timedelta(hours=1))
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="day two - a")
        DirectMessage.objects.filter(body="day two - a").update(created=base + timedelta(days=1))

        content = self._render()
        self.assertEqual(content.count("dm-day-sep"), 2)

    def test_no_separator_between_same_day_messages(self) -> None:
        from datetime import timedelta

        from django.utils import timezone

        base = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
        for i in range(3):
            DirectMessage.objects.create(sender=self.partner, recipient=self.me, body=f"same day {i}")
            DirectMessage.objects.filter(body=f"same day {i}").update(created=base + timedelta(minutes=i))

        content = self._render()
        self.assertEqual(content.count("dm-day-sep"), 1)

    def test_every_message_renders_its_own_menu_button_and_timestamp(self) -> None:
        """The controls/timestamps aren't literally absent - they're always in the
        DOM, just CSS-hidden until hover/tap (verified separately in _messages.scss:
        opacity: 0 with a :hover/:focus-within/--peek reveal). A regression that
        actually removed them from the markup would be a much more severe bug than
        "hidden until hover" - this guards against that happening by accident."""
        DirectMessage.objects.create(sender=self.partner, recipient=self.me, body="one")
        DirectMessage.objects.create(sender=self.me, recipient=self.partner, body="two")

        content = self._render()
        self.assertEqual(content.count("dm-bubble__menu-btn"), 2)
        self.assertEqual(content.count("dm-bubble__time"), 2)


class ThreadMapAttachmentRenderingTests(TestCase):
    """Each map-carrying message must render its own snapshot/dialog DOM ids.

    Regression: `_message_items.html` built the viewer id with
    `"dm-"|add:message.id`. Django's `add` filter returns '' when asked to
    concatenate a str and an int, so every map message in a thread shared the
    same empty id suffix - `getElementById` then resolved every thumbnail and
    dialog to the *first* map's snapshot, making a second sent map display as
    a duplicate of the first (while being stored correctly server-side).
    """

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)
        self.me.ensure_slug()
        self.partner.ensure_slug()
        self.client.force_login(self.me.user)

    def _render(self) -> str:
        response = self.client.get(
            reverse("messages.conversation", kwargs={"profile_slug": self.partner.slug}),
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_two_map_messages_render_distinct_snapshot_and_dialog_ids(self) -> None:
        first_map = MarkupMap.objects.create(profile=self.me, title="First map")
        second_map = MarkupMap.objects.create(profile=self.me, title="Second map")
        first_msg = create_direct_message(self.me, self.partner, "", markup_map_uuid=str(first_map.uuid))
        second_msg = create_direct_message(self.me, self.partner, "", markup_map_uuid=str(second_map.uuid))

        content = self._render()
        self.assertIn(f'id="comment-map-data-dm-{first_msg.pk}"', content)
        self.assertIn(f'id="comment-map-data-dm-{second_msg.pk}"', content)
        self.assertIn(f'id="comment-map-dialog-dm-{first_msg.pk}"', content)
        self.assertIn(f'id="comment-map-dialog-dm-{second_msg.pk}"', content)
        # The broken `"dm-"|add:message.id` produced an empty suffix for every
        # message - assert it never comes back.
        self.assertNotIn('id="comment-map-data-"', content)
        self.assertNotIn('id="comment-map-dialog-"', content)


class NotificationChannelPreferenceTests(TestCase):
    """The `message` delivery preference actually selects the channels used.

    Regression: _notify_recipient only skipped the in-app row for NONE, so a
    user who chose "Email" (not "Notification and email") still got bell
    notifications. EMAIL must mean email only; the messages icon's unread
    badge still reflects the message either way (it counts DirectMessage
    rows, not NotificationLog rows).
    """

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    def _message_notifications(self):
        return NotificationLog.objects.filter(profile=self.recipient, notification_type=NotificationType.MESSAGE)

    def _set_pref(self, value: str) -> None:
        NotificationPreference.objects.update_or_create(profile=self.recipient, defaults={"message": value})

    def test_site_pref_creates_in_app_row(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import DeliveryPreference

        self._set_pref(DeliveryPreference.SITE)
        create_direct_message(self.sender, self.recipient, "hi")
        self.assertEqual(self._message_notifications().count(), 1)

    def test_both_pref_creates_in_app_row(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import DeliveryPreference

        self._set_pref(DeliveryPreference.BOTH)
        create_direct_message(self.sender, self.recipient, "hi")
        self.assertEqual(self._message_notifications().count(), 1)

    def test_email_only_pref_creates_no_in_app_row(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import DeliveryPreference

        self._set_pref(DeliveryPreference.EMAIL)
        create_direct_message(self.sender, self.recipient, "hi")
        self.assertEqual(self._message_notifications().count(), 0)

    def test_none_pref_creates_no_in_app_row(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import DeliveryPreference

        self._set_pref(DeliveryPreference.NONE)
        create_direct_message(self.sender, self.recipient, "hi")
        self.assertEqual(self._message_notifications().count(), 0)

    def test_notification_title_masks_a_sender_the_recipient_cant_view(self) -> None:
        """Regression: the thread itself anonymizes a sender the recipient has
        no standing access to (display_identity_for), but this notification's
        title used the raw username directly - exposing the hidden sender in
        the bell/dropdown before the anonymized thread was ever opened.
        """
        from urbanlens.dashboard.services.direct_messages import display_identity_for

        Profile.objects.filter(pk=self.sender.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        self.sender.refresh_from_db()
        create_direct_message(self.sender, self.recipient, "hi")
        notification = self._message_notifications().get()
        expected_name = display_identity_for(self.recipient, self.sender)["display_name"]
        self.assertNotIn(self.sender.username, notification.title)
        self.assertIn(expected_name, notification.title)


class MessageTextAlertTests(TestCase):
    """The message_whatsapp/message_sms toggles actually deliver.

    Regression: the preference booleans were stored and settable but no
    delivery code ever read them - enabling "new message -> WhatsApp/SMS"
    silently did nothing. The send path now schedules a delayed task
    (mirroring the delayed-email flow) that re-checks unreadness and a
    per-streak debounce before dispatching through notification_delivery.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    def _set_toggles(self, *, whatsapp: bool = False, sms: bool = False) -> None:
        NotificationPreference.objects.update_or_create(profile=self.recipient, defaults={"message_whatsapp": whatsapp, "message_sms": sms})

    def test_send_schedules_the_text_alert_task_when_enabled(self) -> None:
        self._set_toggles(whatsapp=True)
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue:
            create_direct_message(self.sender, self.recipient, "hi")
        scheduled = {call.args[0].__name__ for call in mock_enqueue.call_args_list}
        self.assertIn("send_direct_message_text_alerts_if_unread", scheduled)

    def test_send_does_not_schedule_when_both_toggles_off(self) -> None:
        self._set_toggles()
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as mock_enqueue:
            create_direct_message(self.sender, self.recipient, "hi")
        scheduled = {call.args[0].__name__ for call in mock_enqueue.call_args_list}
        self.assertNotIn("send_direct_message_text_alerts_if_unread", scheduled)

    def test_alert_dispatches_per_enabled_channel_and_sets_debounce(self) -> None:
        from urbanlens.dashboard.services.direct_messages import is_text_alert_debounced, send_message_text_alerts_now

        self._set_toggles(whatsapp=True, sms=False)
        message = create_direct_message(self.sender, self.recipient, "hi")
        with (
            patch("urbanlens.dashboard.services.notification_delivery.send_whatsapp") as mock_wa,
            patch("urbanlens.dashboard.services.notification_delivery.send_sms") as mock_sms,
        ):
            send_message_text_alerts_now(message)
        mock_wa.assert_called_once()
        mock_sms.assert_not_called()
        self.assertTrue(is_text_alert_debounced(self.sender.pk, self.recipient.pk))

    def test_alert_body_never_contains_message_content(self) -> None:
        from urbanlens.dashboard.services.direct_messages import send_message_text_alerts_now

        Profile.objects.filter(pk=self.sender.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        self.sender.refresh_from_db()
        self._set_toggles(sms=True)
        message = create_direct_message(self.sender, self.recipient, "secret rooftop door code 4711")
        with patch("urbanlens.dashboard.services.notification_delivery.send_sms") as mock_sms:
            send_message_text_alerts_now(message)
        body = mock_sms.call_args.args[1]
        self.assertNotIn("4711", body)
        self.assertIn(self.sender.username, body)

    def test_alert_body_masks_a_sender_the_recipient_cant_view(self) -> None:
        """Same recipient-scoped masking as the thread/bell/email paths - the
        text alert goes out-of-band through a carrier, so a hidden sender's
        real username must not appear there either."""
        from urbanlens.dashboard.services.direct_messages import display_identity_for, send_message_text_alerts_now

        Profile.objects.filter(pk=self.sender.pk).update(profile_visibility=VisibilityChoice.NO_ONE)
        self.sender.refresh_from_db()
        self._set_toggles(sms=True)
        message = create_direct_message(self.sender, self.recipient, "hi")
        with patch("urbanlens.dashboard.services.notification_delivery.send_sms") as mock_sms:
            send_message_text_alerts_now(message)
        body = mock_sms.call_args.args[1]
        self.assertNotIn(self.sender.username, body)
        self.assertIn(display_identity_for(self.recipient, self.sender)["display_name"], body)

    def test_task_no_ops_once_the_message_is_read(self) -> None:
        from django.utils import timezone

        from urbanlens.dashboard.tasks import send_direct_message_text_alerts_if_unread

        self._set_toggles(whatsapp=True)
        message = create_direct_message(self.sender, self.recipient, "hi")
        DirectMessage.objects.filter(pk=message.pk).update(read_at=timezone.now())
        with patch("urbanlens.dashboard.services.notification_delivery.send_whatsapp") as mock_wa:
            send_direct_message_text_alerts_if_unread(message.pk)
        mock_wa.assert_not_called()


class SelfDeletedMessageVisibilityTests(TestCase):
    """Messages deleted-for-self stay out of the sidebar preview and unread badge.

    Regression: conversation_rows/unread_conversation_count ran on the raw
    involving() set, so a message the recipient had removed from their own
    view could still light the navbar badge and surface as the sidebar's
    last-message preview.
    """

    def setUp(self) -> None:
        super().setUp()
        self.me = _profile()
        self.partner = _profile()
        _set_dm_visibility(self.me, VisibilityChoice.ANYONE)
        _set_dm_visibility(self.partner, VisibilityChoice.ANYONE)

    def test_self_deleted_unread_message_does_not_count_as_unread(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_self

        message = create_direct_message(self.partner, self.me, "hi")
        self.assertEqual(DirectMessage.objects.unread_conversation_count(self.me), 1)
        delete_message_for_self(message, self.me)
        self.assertEqual(DirectMessage.objects.unread_conversation_count(self.me), 0)

    def test_self_deleted_message_is_not_the_sidebar_preview(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_self

        first = create_direct_message(self.partner, self.me, "keep me")
        second = create_direct_message(self.partner, self.me, "hide me")
        delete_message_for_self(second, self.me)
        conversations = conversations_for(self.me)
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["last_message"].pk, first.pk)

    def test_conversation_disappears_when_every_message_is_self_deleted(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_self

        only = create_direct_message(self.partner, self.me, "hi")
        delete_message_for_self(only, self.me)
        self.assertEqual(conversations_for(self.me), [])

    def test_self_deleted_message_stays_out_of_the_thread_page_too(self) -> None:
        """Regression: thread_page() (reopening/scrolling a conversation) queried
        between() directly, without visible_to() - so a "remove for me" delete
        stuck in the sidebar/badge but reappeared the moment the thread reloaded.
        """
        from urbanlens.dashboard.services.direct_messages import delete_message_for_self, thread_page

        keep = create_direct_message(self.partner, self.me, "keep me")
        hide = create_direct_message(self.partner, self.me, "hide me")
        delete_message_for_self(hide, self.me)
        messages, _has_more = thread_page(self.me, self.partner)
        self.assertIn(keep, messages)
        self.assertNotIn(hide, messages)
        # The sender still sees their own message in their own thread view.
        partner_messages, _has_more = thread_page(self.partner, self.me)
        self.assertIn(hide, partner_messages)


class SenderOwnDeletedForEveryoneVisibilityTests(TestCase):
    """visible_to() must never hide a sender's own sent message from themselves.

    Regression found while adding SelfDeletedMessageVisibilityTests above:
    the pre-existing `visible_to()` filter gated the SENDER's own rows on
    `deleted_by_sender_at__isnull=True` too - so once a sender used "delete
    for everyone" (which only tombstones the RECIPIENT's view -
    tombstone_text_for always returns None for the sender), any call site
    built on visible_to() silently dropped that message from the sender's
    OWN results. Applying visible_to() to conversations_for() (this session's
    fix for self-deleted-by-recipient leaking into the sidebar) turned that
    into a much bigger problem: a sender's entire conversation could vanish
    from their own sidebar once every message in it had been "deleted for
    everyone" by them. The filter itself is fixed so `deleted_by_sender_at`
    never gates the sender's own view anywhere - only `deleted_by_recipient_at`
    gates the recipient's own view.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        _set_dm_visibility(self.recipient, VisibilityChoice.ANYONE)

    def test_visible_to_never_excludes_the_senders_own_deleted_for_everyone_row(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_everyone

        message = create_direct_message(self.sender, self.recipient, "hi")
        delete_message_for_everyone(message, self.sender)
        self.assertIn(message, DirectMessage.objects.visible_to(self.sender))

    def test_conversation_stays_in_the_senders_sidebar_after_deleting_for_everyone(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_everyone

        message = create_direct_message(self.sender, self.recipient, "hi")
        delete_message_for_everyone(message, self.sender)
        conversations = conversations_for(self.sender)
        self.assertEqual(len(conversations), 1)
        self.assertEqual(conversations[0]["last_message"].pk, message.pk)

    def test_recipient_still_only_sees_the_tombstone(self) -> None:
        """The fix must not resurrect the message's content for the recipient."""
        from urbanlens.dashboard.services.direct_messages import delete_message_for_everyone

        message = create_direct_message(self.sender, self.recipient, "hi")
        delete_message_for_everyone(message, self.sender)
        self.assertIn(message, DirectMessage.objects.visible_to(self.recipient))
        self.assertEqual(message.tombstone_text_for(self.recipient.pk), "Message deleted")

    def test_recipient_self_delete_is_still_excluded_from_their_own_view(self) -> None:
        """The fix only changes the sender-side gate - deleted_by_recipient_at still hides it."""
        from urbanlens.dashboard.services.direct_messages import delete_message_for_self

        message = create_direct_message(self.sender, self.recipient, "hi")
        delete_message_for_self(message, self.recipient)
        self.assertNotIn(message, DirectMessage.objects.visible_to(self.recipient))
        self.assertIn(message, DirectMessage.objects.visible_to(self.sender))
