"""The friend-invite-by-email response must not reveal account existence."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
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
