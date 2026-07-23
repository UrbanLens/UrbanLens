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
    tests hit friend.page_widget - the endpoint that actually renders
    friends_page_content.html. (An earlier revision requested friend.list
    with an HX-Target header instead, but that action never dispatched on
    the header - it always renders the compact partial, which contains no
    pending section at all, so those assertions were passing/failing against
    the wrong markup entirely.)
    """

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="widgetinviter", email="widgetinviter@example.com")
        self.client.force_login(self.inviter)

    def _friend_list_url(self, user: User | None = None) -> str:
        return reverse("friend.list", kwargs={"profile_id": (user or self.inviter).profile.id})

    def _widget_url(self, user: User | None = None) -> str:
        return reverse("friend.page_widget", kwargs={"profile_id": (user or self.inviter).profile.id})

    def test_registered_target_identity_is_hidden_in_the_pending_widget(self) -> None:
        target = baker.make(User, username="secretusername", email="target@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})

        response = self.client.get(self._widget_url())

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

        response = self.client.get(self._widget_url())

        self.assertNotIn(b"directtarget", response.content)
        self.assertIn(b"Pending request", response.content)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_registered_and_unregistered_pending_entries_render_identically(self, mock_send) -> None:
        target = baker.make(User, username="realuser2", email="target2@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})
        registered_response = self.client.get(self._widget_url()).content

        other_inviter = baker.make(User, username="widgetinviter2", email="widgetinviter2@example.com")
        self.client.force_login(other_inviter)
        self.client.post(reverse("friend.invite_email"), {"email": "brandnew-unmatched@example.com"})
        unregistered_response = self.client.get(self._widget_url(other_inviter)).content

        self.assertIn(b'<span class="badge">1</span>', registered_response)
        self.assertIn(b'<span class="badge">1</span>', unregistered_response)
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

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_pending_cards_carry_no_type_revealing_urls_or_ids(self, mock_send) -> None:
        """The cancel buttons must not distinguish the two pending kinds.

        The first version of this widget rendered identical card BODIES but
        posted matched-email cancels to friend.remove/<target_profile_id>
        and unmatched ones to a separate cancel-invitation/<pk> URL - so the
        DOM still told the sender whether the email belonged to an account
        (and, worse, the target's profile id). Both kinds must share the one
        opaque cancel_pending URL shape and neither legacy URL may appear.
        """
        target = baker.make(User, username="urlleaktarget", email="urlleak@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})
        self.client.post(reverse("friend.invite_email"), {"email": "urlleak-unmatched@example.com"})

        content = self.client.get(self._widget_url()).content.decode()

        self.assertNotIn(f"/remove/{target.profile.pk}", content)
        self.assertNotIn("cancel-invitation", content)
        self.assertEqual(content.count("/pending/"), 2)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_pending_cards_are_structurally_identical_across_kinds(self, mock_send) -> None:
        """Modulo the opaque token itself, a matched-email card and an
        unmatched-email card must render byte-identically - including their
        ORDER being chronological rather than grouped by kind, which would
        otherwise leak the kind of any card via its position."""
        import re

        target = baker.make(User, username="structuretarget", email="structure@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})
        matched_only = self.client.get(self._widget_url()).content.decode()

        other_inviter = baker.make(User, username="structureinviter2", email="structureinviter2@example.com")
        self.client.force_login(other_inviter)
        self.client.post(reverse("friend.invite_email"), {"email": "structure-unmatched@example.com"})
        unmatched_only = self.client.get(self._widget_url(other_inviter)).content.decode()

        def pending_section(content: str) -> str:
            match = re.search(r'<ul class="friend-request-list friend-request-list--page">.*?</ul>', content, flags=re.DOTALL)
            assert match is not None
            return re.sub(r"/pending/[0-9a-f]+/cancel/", "/pending/TOKEN/cancel/", match.group(0))

        self.assertEqual(pending_section(matched_only), pending_section(unmatched_only))


class CancelPendingViewTests(TestCase):
    """The unified opaque-token cancel endpoint for pending outgoing requests
    of BOTH kinds (Friendship and FriendInvitation) - see _pending_cancel_token."""

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="cancelinviter", email="cancelinviter@example.com")
        self.client.force_login(self.inviter)

    def _token(self, kind: str, pk: int, profile=None) -> str:
        from urbanlens.dashboard.controllers.friendship import _pending_cancel_token

        return _pending_cancel_token((profile or self.inviter.profile).pk, kind, pk)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_cancel_deletes_a_pending_invitation(self, mock_send) -> None:
        self.client.post(reverse("friend.invite_email"), {"email": "cancel-me@example.com"})
        invitation = FriendInvitation.objects.get(inviter=self.inviter.profile, email="cancel-me@example.com")

        response = self.client.post(reverse("friend.cancel_pending", kwargs={"token": self._token("invitation", invitation.pk)}), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FriendInvitation.objects.filter(pk=invitation.pk).exists())

    def test_cancel_removes_a_pending_friendship_request(self) -> None:
        target = baker.make(User, username="cancelfriendtarget", email="cancelfriendtarget@example.com", is_active=True)
        friendship = Friendship.objects.create(from_profile=self.inviter.profile, to_profile=target.profile, status=FriendshipStatus.REQUESTED)

        response = self.client.post(reverse("friend.cancel_pending", kwargs={"token": self._token("friendship", friendship.pk)}), HTTP_HX_REQUEST="true")

        self.assertEqual(response.status_code, 200)
        friendship.refresh_from_db()
        self.assertNotEqual(friendship.status, FriendshipStatus.REQUESTED)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_cannot_cancel_someone_elses_invitation(self, mock_send) -> None:
        self.client.post(reverse("friend.invite_email"), {"email": "cancel-me2@example.com"})
        invitation = FriendInvitation.objects.get(inviter=self.inviter.profile, email="cancel-me2@example.com")

        other_user = baker.make(User, username="notowner", email="notowner@example.com")
        self.client.force_login(other_user)

        # Even the RIGHT token for the row 404s for the wrong caller: tokens
        # are scoped to the sender's own profile pk, so the other user's
        # recomputed set can never contain this one.
        response = self.client.post(reverse("friend.cancel_pending", kwargs={"token": self._token("invitation", invitation.pk)}))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(FriendInvitation.objects.filter(pk=invitation.pk).exists())

    def test_cancelling_with_a_garbage_token_returns_404(self) -> None:
        response = self.client.post(reverse("friend.cancel_pending", kwargs={"token": "a" * 40}))
        self.assertEqual(response.status_code, 404)

    def test_accepted_friendship_is_not_cancellable_via_token(self) -> None:
        """Only REQUESTED rows are in the recomputed set - an accepted
        friendship's token must not resolve (unfriending has its own flow)."""
        target = baker.make(User, username="acceptedtarget", email="acceptedtarget@example.com", is_active=True)
        friendship = Friendship.objects.create(from_profile=self.inviter.profile, to_profile=target.profile, status=FriendshipStatus.ACCEPTED)

        response = self.client.post(reverse("friend.cancel_pending", kwargs={"token": self._token("friendship", friendship.pk)}))

        self.assertEqual(response.status_code, 404)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)
