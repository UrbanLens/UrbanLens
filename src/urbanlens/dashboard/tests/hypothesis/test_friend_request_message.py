"""Tests for the optional message attached to friend requests and email invites."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import VisibilityChoice


class DirectFriendRequestMessageTests(TestCase):
    def setUp(self) -> None:
        self.requester = baker.make(User, username="requester")
        self.target = baker.make(User, username="target", is_active=True)
        self.target.profile.friend_request_visibility = VisibilityChoice.ANYONE
        self.target.profile.save(update_fields=["friend_request_visibility"])
        self.client.force_login(self.requester)
        self.url = reverse("friend.request", args=[self.target.profile.id])

    def test_message_is_stored_on_the_friendship(self) -> None:
        self.client.post(self.url, {"message": "Hey, we met at the abandoned mill!"})

        friendship = Friendship.objects.all().between(self.requester.profile, self.target.profile)
        self.assertEqual(friendship.request_message, "Hey, we met at the abandoned mill!")

    def test_message_is_included_in_the_notification(self) -> None:
        self.client.post(self.url, {"message": "Hey, we met at the abandoned mill!"})

        notification = NotificationLog.objects.get(profile=self.target.profile, notification_type=NotificationType.FRIEND_REQUEST)
        self.assertIn("Hey, we met at the abandoned mill!", notification.message)

    def test_request_without_a_message_still_works(self) -> None:
        response = self.client.post(self.url, {})

        self.assertEqual(response.status_code, 302)
        friendship = Friendship.objects.all().between(self.requester.profile, self.target.profile)
        self.assertIsNone(friendship.request_message)

    def test_overlong_message_is_rejected(self) -> None:
        response = self.client.post(self.url, {"message": "x" * 1001})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Friendship.objects.all().between(self.requester.profile, self.target.profile))


class EmailInviteMessageTests(TestCase):
    def setUp(self) -> None:
        self.inviter = baker.make(User, username="inviter", email="inviter@example.com")
        self.client.force_login(self.inviter)
        self.url = reverse("friend.invite_email")

    def test_message_is_stored_on_the_friendship_for_an_existing_user(self) -> None:
        target = baker.make(User, username="realuser", email="target@example.com", is_active=True)

        self.client.post(self.url, {"email": target.email, "message": "Join me on UrbanLens!"})

        friendship = Friendship.objects.all().between(self.inviter.profile, target.profile)
        self.assertEqual(friendship.request_message, "Join me on UrbanLens!")

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_message_is_stored_on_the_invitation_for_a_new_email(self, mock_send) -> None:
        self.client.post(self.url, {"email": "brandnew@example.com", "message": "Come check out UrbanLens!"})

        invitation = FriendInvitation.objects.get(inviter=self.inviter.profile, email="brandnew@example.com")
        self.assertEqual(invitation.message, "Come check out UrbanLens!")

    @patch("django.core.mail.EmailMultiAlternatives")
    def test_message_appears_in_the_sent_email_body(self, mock_email_cls) -> None:
        self.client.post(self.url, {"email": "brandnew@example.com", "message": "Come check out UrbanLens!"})

        _args, kwargs = mock_email_cls.call_args
        self.assertIn("Come check out UrbanLens!", kwargs["body"])
        mock_email_cls.return_value.attach_alternative.assert_called_once()
        html_body = mock_email_cls.return_value.attach_alternative.call_args[0][0]
        self.assertIn("Come check out UrbanLens!", html_body)

    def test_overlong_message_is_rejected(self) -> None:
        response = self.client.post(self.url, {"email": "someone@example.com", "message": "x" * 1001})

        self.assertEqual(response.status_code, 400)

    def test_invitation_message_carries_through_to_signup_auto_friend_request(self) -> None:
        FriendInvitation.objects.create(inviter=self.inviter.profile, email="newperson@example.com", message="Welcome aboard!")

        new_user = baker.make(User, username="newperson", email="newperson@example.com", is_active=True)
        from urbanlens.dashboard.controllers.account import _process_pending_invitations

        _process_pending_invitations(new_user)

        friendship = Friendship.objects.all().between(self.inviter.profile, new_user.profile)
        self.assertIsNotNone(friendship)
        self.assertEqual(friendship.request_message, "Welcome aboard!")
