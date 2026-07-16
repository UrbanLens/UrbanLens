"""The friend-invite-by-email response must not reveal account existence."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship import FriendshipStatus
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import VisibilityChoice


class InviteByEmailPrivacyTests(TestCase):
    """Registered vs. unregistered emails must produce an identical response."""

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="inviter", email="inviter@example.com")
        self.client.force_login(self.inviter)
        self.url = reverse("friend.invite_email")

    def test_response_body_identical_for_existing_and_nonexistent_email(self) -> None:
        target = baker.make(User, username="realuser", email="target@example.com", is_active=True)

        resp_existing = self.client.post(self.url, {"email": target.email})
        resp_missing = self.client.post(self.url, {"email": "nobody-here@example.com"})

        self.assertEqual(resp_existing.status_code, resp_missing.status_code)
        self.assertEqual(resp_existing.content, resp_missing.content)

    def test_response_does_not_contain_target_username(self) -> None:
        target = baker.make(User, username="secretusername", email="target@example.com", is_active=True)

        response = self.client.post(self.url, {"email": target.email})

        self.assertNotIn(b"secretusername", response.content)

    def test_response_identical_regardless_of_target_friend_request_visibility(self) -> None:
        open_target = baker.make(User, username="openuser", email="open@example.com", is_active=True)
        closed_target = baker.make(User, username="closeduser", email="closed@example.com", is_active=True)
        closed_target.profile.friend_request_visibility = VisibilityChoice.NO_ONE
        closed_target.profile.save(update_fields=["friend_request_visibility"])

        resp_open = self.client.post(self.url, {"email": open_target.email})
        resp_closed = self.client.post(self.url, {"email": closed_target.email})

        self.assertEqual(resp_open.status_code, resp_closed.status_code)
        self.assertEqual(resp_open.content, resp_closed.content)
        # The request should have actually gone through for the open target...
        self.assertTrue(Friendship.objects.filter(from_profile=self.inviter.profile, to_profile=open_target.profile).exists())
        # ...but silently not for the one who disabled friend requests.
        self.assertFalse(Friendship.objects.filter(from_profile=self.inviter.profile, to_profile=closed_target.profile).exists())

    def test_existing_user_actually_receives_friend_request(self) -> None:
        target = baker.make(User, username="realuser", email="target@example.com", is_active=True)

        self.client.post(self.url, {"email": target.email})

        self.assertTrue(Friendship.objects.filter(from_profile=self.inviter.profile, to_profile=target.profile).exists())

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_nonexistent_user_gets_invitation_record(self, mock_send) -> None:
        self.client.post(self.url, {"email": "brandnew@example.com"})

        self.assertTrue(FriendInvitation.objects.filter(inviter=self.inviter.profile, email="brandnew@example.com").exists())

    def test_gmail_variant_of_existing_email_is_matched(self) -> None:
        target = baker.make(User, username="realuser", email="jakesmith@gmail.com", is_active=True)

        self.client.post(self.url, {"email": "Jake.Smith+invite@gmail.com"})

        self.assertTrue(Friendship.objects.filter(from_profile=self.inviter.profile, to_profile=target.profile).exists())

    def test_own_email_is_rejected(self) -> None:
        response = self.client.post(self.url, {"email": self.inviter.email})
        self.assertEqual(response.status_code, 400)

    def test_invalid_email_is_rejected(self) -> None:
        response = self.client.post(self.url, {"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)


class OutgoingRequestWidgetPrivacyTests(TestCase):
    """The sender's own "pending sent requests" widget must not reveal the
    target's identity, nor whether an invited email matched a registered
    account, until the request is accepted - see _friend_list_ctx's docstring.

    The widget only renders on the "View all friends" page now (the compact
    profile-page embed dropped it - see friend_list_partial.html), so these
    requests set the same HX-Target header that page's own refresh uses.
    """

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="widgetinviter", email="widgetinviter@example.com")
        self.client.force_login(self.inviter)

    def _friend_list_url(self, user: User | None = None) -> str:
        return reverse("friend.list", kwargs={"profile_id": (user or self.inviter).profile.id})

    def test_registered_target_identity_is_hidden_in_the_pending_widget(self) -> None:
        target = baker.make(User, username="secretusername", email="target@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})

        response = self.client.get(self._friend_list_url(), HTTP_HX_TARGET="friends_page_list")

        self.assertNotIn(b"secretusername", response.content)
        self.assertNotIn(b"target@example.com", response.content)
        self.assertIn(b"Pending request", response.content)

    def test_direct_friend_request_identity_is_also_hidden_until_accepted(self) -> None:
        """Even a request sent by clicking "Add Friend" on a visible profile - where
        the sender already knows who they requested - must render generically here,
        so the widget's shape can never be used to distinguish that case from an
        email-guess request (which the sender should NOT be able to identify)."""
        target = baker.make(User, username="directtarget", email="direct@example.com", is_active=True)
        Friendship.objects.create(from_profile=self.inviter.profile, to_profile=target.profile, status=FriendshipStatus.REQUESTED)

        response = self.client.get(self._friend_list_url(), HTTP_HX_TARGET="friends_page_list")

        self.assertNotIn(b"directtarget", response.content)
        self.assertIn(b"Pending request", response.content)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_registered_and_unregistered_pending_entries_render_identically(self, mock_send) -> None:
        target = baker.make(User, username="realuser2", email="target2@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})
        registered_response = self.client.get(self._friend_list_url(), HTTP_HX_TARGET="friends_page_list").content

        other_inviter = baker.make(User, username="widgetinviter2", email="widgetinviter2@example.com")
        self.client.force_login(other_inviter)
        self.client.post(reverse("friend.invite_email"), {"email": "brandnew-unmatched@example.com"})
        unregistered_response = self.client.get(self._friend_list_url(other_inviter), HTTP_HX_TARGET="friends_page_list").content

        self.assertIn(b"1 pending sent request", registered_response)
        self.assertIn(b"1 pending sent request", unregistered_response)
        self.assertIn(b"Pending request", registered_response)
        self.assertIn(b"Pending request", unregistered_response)

    def test_pending_widget_does_not_show_on_the_compact_profile_embed(self) -> None:
        """The widget was removed from the main profile page's compact friend list -
        it only remains on the dedicated "View all friends" page (see the class docstring)."""
        other = baker.make(User, username="compacttarget", email="compacttarget@example.com")
        Friendship.objects.create(from_profile=self.inviter.profile, to_profile=other.profile, status=FriendshipStatus.REQUESTED)

        response = self.client.get(self._friend_list_url())

        self.assertNotIn(b"pending sent request", response.content)

    def test_outgoing_pending_count_sums_both_request_types(self) -> None:
        from urbanlens.dashboard.controllers.friendship import _friend_list_ctx

        other = baker.make(User, username="counterother", email="counterother@example.com")
        Friendship.objects.create(from_profile=self.inviter.profile, to_profile=other.profile, status=FriendshipStatus.REQUESTED)
        FriendInvitation.objects.create(inviter=self.inviter.profile, email="unmatched-count@example.com")

        ctx = _friend_list_ctx(self.inviter.profile, self.inviter.profile)

        self.assertEqual(ctx["outgoing_pending_count"], 2)


class CancelInvitationViewTests(TestCase):
    """Cancelling a pending email invitation - the FriendInvitation counterpart
    to friend.remove for a Friendship request."""

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="cancelinviter", email="cancelinviter@example.com")
        self.client.force_login(self.inviter)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_cancel_deletes_the_invitation(self, mock_send) -> None:
        self.client.post(reverse("friend.invite_email"), {"email": "cancel-me@example.com"})
        invitation = FriendInvitation.objects.get(inviter=self.inviter.profile, email="cancel-me@example.com")

        response = self.client.post(reverse("friend.cancel_invitation", kwargs={"invitation_id": invitation.id}), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FriendInvitation.objects.filter(pk=invitation.pk).exists())

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_cannot_cancel_someone_elses_invitation(self, mock_send) -> None:
        self.client.post(reverse("friend.invite_email"), {"email": "cancel-me2@example.com"})
        invitation = FriendInvitation.objects.get(inviter=self.inviter.profile, email="cancel-me2@example.com")

        other_user = baker.make(User, username="notowner", email="notowner@example.com")
        self.client.force_login(other_user)

        response = self.client.post(reverse("friend.cancel_invitation", kwargs={"invitation_id": invitation.id}))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(FriendInvitation.objects.filter(pk=invitation.pk).exists())

    def test_cancelling_an_unknown_invitation_returns_404(self) -> None:
        response = self.client.post(reverse("friend.cancel_invitation", kwargs={"invitation_id": 999999}))
        self.assertEqual(response.status_code, 404)
