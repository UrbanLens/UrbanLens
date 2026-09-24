"""The friend-invite-by-email response must not reveal account existence."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship import FriendshipStatus
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.tasks import deliver_friend_invitation


def make_invitable_user(**kwargs) -> User:
    """Bake a user with a verified primary address who accepts friend requests from anyone.

    ``friend_request_visibility`` defaults to ``ANYONE``, so this is a no-op against a freshly baked profile
    today - but it makes each test's dependency on that setting explicit rather than incidental, and keeps these
    tests correct if the default (or model_bakery's field-generation behavior) ever changes again.

    Args:
        **kwargs: Passed through to ``baker.make(User, ...)``.

    Returns:
        The baked user, with friend requests open to anyone."""
    user = baker.make(User, **kwargs)
    user.profile.friend_request_visibility = VisibilityChoice.ANYONE
    user.profile.verified_primary_email = user.profile.primary_email_normalized
    user.profile.save(update_fields=["friend_request_visibility", "verified_primary_email"])
    return user


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
        # This test is about the gate, so both ends are set explicitly.
        open_target = make_invitable_user(username="openuser", email="open@example.com", is_active=True)
        closed_target = baker.make(User, username="closeduser", email="closed@example.com", is_active=True)
        closed_target.profile.friend_request_visibility = VisibilityChoice.NO_ONE
        closed_target.profile.save(update_fields=["friend_request_visibility"])

        with tasks_run_inline(deliver_friend_invitation), self.captureOnCommitCallbacks(execute=True):
            resp_open = self.client.post(self.url, {"email": open_target.email})
            resp_closed = self.client.post(self.url, {"email": closed_target.email})

        self.assertEqual(resp_open.status_code, resp_closed.status_code)
        self.assertEqual(resp_open.content, resp_closed.content)
        # The request should have actually gone through for the open target...
        self.assertTrue(
            FriendInvitation.objects.filter(inviter=self.inviter.profile, invitee=open_target.profile).exists()
        )
        # ...but silently not for the one who disabled friend requests.
        self.assertFalse(
            FriendInvitation.objects.filter(inviter=self.inviter.profile, invitee=closed_target.profile).exists()
        )

    def test_existing_user_actually_receives_friend_request(self) -> None:
        target = make_invitable_user(username="realuser", email="target@example.com", is_active=True)

        with tasks_run_inline(deliver_friend_invitation), self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"email": target.email})

        self.assertTrue(FriendInvitation.objects.filter(inviter=self.inviter.profile, invitee=target.profile).exists())

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_nonexistent_user_gets_invitation_record(self, mock_send) -> None:
        self.client.post(self.url, {"email": "brandnew@example.com"})

        self.assertTrue(
            FriendInvitation.objects.filter(inviter=self.inviter.profile, email="brandnew@example.com").exists()
        )

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_reinviting_a_gmail_variant_replaces_the_pending_invitation(self, mock_send) -> None:
        """A dot/+ variant of an already-invited address must dedupe against the same pending FriendInvitation row, not create a second one - otherwise the invitee's eventual signup only auto-accepts whichever row happens to match their exact registered spelling."""
        self.client.post(self.url, {"email": "johndoe3@gmail.com"})
        self.client.post(self.url, {"email": "John.Doe.3+invite@gmail.com"})

        self.assertEqual(
            FriendInvitation.objects.filter(inviter=self.inviter.profile, accepted_at__isnull=True).count(),
            1,
        )

    def test_gmail_variant_of_existing_email_is_matched(self) -> None:
        target = make_invitable_user(username="realuser", email="jakesmith@gmail.com", is_active=True)

        with tasks_run_inline(deliver_friend_invitation), self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, {"email": "Jake.Smith+invite@gmail.com"})

        self.assertTrue(FriendInvitation.objects.filter(inviter=self.inviter.profile, invitee=target.profile).exists())

    def test_own_email_is_rejected(self) -> None:
        response = self.client.post(self.url, {"email": self.inviter.email})
        self.assertEqual(response.status_code, 400)

    def test_invalid_email_is_rejected(self) -> None:
        response = self.client.post(self.url, {"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)


class OutgoingRequestWidgetPrivacyTests(TestCase):
    """The sender's own "pending sent requests" widget must not reveal the target's identity, nor whether an invited email matched a registered account, until the request is accepted - see _friend_list_ctx's docstring.

    The widget only renders on the "View all friends" page now (the compact profile-page embed dropped it - see
    friend_list_partial.html), so these tests hit friend.page_widget - the endpoint that actually renders
    friends_page_content.html."""

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="widgetinviter", email="widgetinviter@example.com")
        self.client.force_login(self.inviter)

    def _friend_list_url(self, user: User | None = None) -> str:
        return reverse("friend.list", kwargs={"profile_id": (user or self.inviter).profile.id})

    def _widget_url(self, user: User | None = None) -> str:
        return reverse("friend.page_widget", kwargs={"profile_id": (user or self.inviter).profile.id})

    def test_registered_target_identity_is_hidden_in_the_pending_widget(self) -> None:
        target = make_invitable_user(username="secretusername", email="target@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})

        response = self.client.get(self._widget_url())

        self.assertNotIn(b"secretusername", response.content)
        self.assertNotIn(b"target@example.com", response.content)
        self.assertIn(b"Pending request", response.content)

    def test_direct_friend_request_identity_is_also_hidden_until_accepted(self) -> None:
        """Even a request sent by clicking "Add Friend" on a visible profile - where the sender already knows who they requested - must render generically here, so the widget's shape can never be used to distinguish that case from an email-guess request (which the sender should NOT be able to identify)."""
        target = baker.make(User, username="directtarget", email="direct@example.com", is_active=True)
        Friendship.objects.create(
            from_profile=self.inviter.profile, to_profile=target.profile, status=FriendshipStatus.REQUESTED
        )

        response = self.client.get(self._widget_url())

        self.assertNotIn(b"directtarget", response.content)
        self.assertIn(b"Pending request", response.content)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_registered_and_unregistered_pending_entries_render_identically(self, mock_send) -> None:
        target = make_invitable_user(username="realuser2", email="target2@example.com", is_active=True)
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
        Friendship.objects.create(
            from_profile=self.inviter.profile, to_profile=other.profile, status=FriendshipStatus.REQUESTED
        )

        response = self.client.get(self._friend_list_url())

        self.assertNotIn(b"pending sent request", response.content)

    def test_outgoing_pending_count_sums_both_request_types(self) -> None:
        from urbanlens.dashboard.controllers.friendship import _friend_list_ctx

        other = baker.make(User, username="counterother", email="counterother@example.com")
        Friendship.objects.create(
            from_profile=self.inviter.profile, to_profile=other.profile, status=FriendshipStatus.REQUESTED
        )
        FriendInvitation.objects.create(inviter=self.inviter.profile, email="unmatched-count@example.com")

        ctx = _friend_list_ctx(self.inviter.profile, self.inviter.profile)

        self.assertEqual(ctx["outgoing_pending_count"], 2)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_pending_cards_carry_no_type_revealing_urls_or_ids(self, mock_send) -> None:
        """The cancel buttons must not distinguish the two pending kinds.

        The first version of this widget rendered identical card BODIES but posted matched-email cancels to
        friend.remove/<target_profile_id> and unmatched ones to a separate cancel-invitation/<pk> URL - so the
        DOM still told the sender whether the email belonged to an account (and, worse, the target's profile
        id)."""
        target = make_invitable_user(username="urlleaktarget", email="urlleak@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})
        self.client.post(reverse("friend.invite_email"), {"email": "urlleak-unmatched@example.com"})

        content = self.client.get(self._widget_url()).content.decode()

        self.assertNotIn(f"/remove/{target.profile.pk}", content)
        self.assertNotIn("cancel-invitation", content)
        self.assertEqual(content.count("/pending/"), 2)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_pending_cards_are_structurally_identical_across_kinds(self, mock_send) -> None:
        """Modulo the opaque token itself, a matched-email card and an unmatched-email card must render byte-identically - including their ORDER being chronological rather than grouped by kind, which would otherwise leak the kind of any card via its position."""
        import re

        target = make_invitable_user(username="structuretarget", email="structure@example.com", is_active=True)
        self.client.post(reverse("friend.invite_email"), {"email": target.email})
        matched_only = self.client.get(self._widget_url()).content.decode()

        other_inviter = baker.make(User, username="structureinviter2", email="structureinviter2@example.com")
        self.client.force_login(other_inviter)
        self.client.post(reverse("friend.invite_email"), {"email": "structure-unmatched@example.com"})
        unmatched_only = self.client.get(self._widget_url(other_inviter)).content.decode()

        def pending_section(content: str) -> str:
            match = re.search(
                r'<ul class="friend-request-list friend-request-list--page">.*?</ul>', content, flags=re.DOTALL
            )
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

        response = self.client.post(
            reverse("friend.cancel_pending", kwargs={"token": self._token("invitation", invitation.pk)}),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FriendInvitation.objects.filter(pk=invitation.pk).exists())

    def test_cancel_removes_a_pending_friendship_request(self) -> None:
        target = baker.make(User, username="cancelfriendtarget", email="cancelfriendtarget@example.com", is_active=True)
        friendship = Friendship.objects.create(
            from_profile=self.inviter.profile, to_profile=target.profile, status=FriendshipStatus.REQUESTED
        )

        response = self.client.post(
            reverse("friend.cancel_pending", kwargs={"token": self._token("friendship", friendship.pk)}),
            HTTP_HX_REQUEST="true",
        )

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
        response = self.client.post(
            reverse("friend.cancel_pending", kwargs={"token": self._token("invitation", invitation.pk)})
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(FriendInvitation.objects.filter(pk=invitation.pk).exists())

    def test_cancelling_with_a_garbage_token_returns_404(self) -> None:
        response = self.client.post(reverse("friend.cancel_pending", kwargs={"token": "a" * 40}))
        self.assertEqual(response.status_code, 404)

    def test_accepted_friendship_is_not_cancellable_via_token(self) -> None:
        """Only REQUESTED rows are in the recomputed set - an accepted
        friendship's token must not resolve (unfriending has its own flow)."""
        target = baker.make(User, username="acceptedtarget", email="acceptedtarget@example.com", is_active=True)
        friendship = Friendship.objects.create(
            from_profile=self.inviter.profile, to_profile=target.profile, status=FriendshipStatus.ACCEPTED
        )

        response = self.client.post(
            reverse("friend.cancel_pending", kwargs={"token": self._token("friendship", friendship.pk)})
        )

        self.assertEqual(response.status_code, 404)
        friendship.refresh_from_db()
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)


class PendingWidgetDoesNotDependOnTheTargetTests(TestCase):
    """Inviting an address that belongs to an account which cannot be requested must still leave a pending entry.

    Otherwise the sender's own pending count tells them the address is registered: an unregistered address
    always adds one.
    """

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="probe", email="probe@example.com")
        self.client.force_login(self.inviter)

    def _pending_after_inviting(self, email: str) -> int:
        from urbanlens.dashboard.controllers.friendship import _friend_list_ctx

        with patch("django.core.mail.EmailMultiAlternatives.send"):
            self.client.post(reverse("friend.invite_email"), {"email": email})
        return _friend_list_ctx(self.inviter.profile, self.inviter.profile)["outgoing_pending_count"]

    def test_an_account_refusing_friend_requests_reads_like_an_unregistered_address(self) -> None:
        closed = baker.make(User, username="closed", email="closed@example.com", is_active=True)
        closed.profile.friend_request_visibility = VisibilityChoice.NO_ONE
        closed.profile.save(update_fields=["friend_request_visibility"])
        self.assertEqual(self._pending_after_inviting(closed.email), 1)
        self.assertFalse(FriendInvitation.objects.filter(inviter=self.inviter.profile, invitee=closed.profile).exists())

    def test_an_account_that_blocked_the_sender_reads_like_an_unregistered_address(self) -> None:
        blocker = make_invitable_user(username="blocker", email="blocker@example.com", is_active=True)
        Friendship.objects.create(
            from_profile=blocker.profile, to_profile=self.inviter.profile, status=FriendshipStatus.BLOCKED
        )
        self.assertEqual(self._pending_after_inviting(blocker.email), 1)

    def test_an_existing_friends_address_reads_like_an_unregistered_address(self) -> None:
        friend = make_invitable_user(username="friend", email="friend@example.com", is_active=True)
        Friendship.objects.create(
            from_profile=self.inviter.profile, to_profile=friend.profile, status=FriendshipStatus.ACCEPTED
        )
        self.assertEqual(self._pending_after_inviting(friend.email), 1)

    def test_the_unregistered_baseline(self) -> None:
        self.assertEqual(self._pending_after_inviting("nobody-at-all@example.com"), 1)


class InviteSideChannelsTests(TestCase):
    """Neither the email budget nor the request's latency may depend on whether the address has an account."""

    def setUp(self) -> None:
        self.inviter = baker.make(User, username="timer", email="timer@example.com")
        self.client.force_login(self.inviter)

    def test_the_email_budget_is_charged_the_same_either_way(self) -> None:
        from urbanlens.dashboard.models.email_log import EmailSendLog

        target = make_invitable_user(username="registered", email="registered@example.com", is_active=True)
        with patch("django.core.mail.EmailMultiAlternatives.send"):
            self.client.post(reverse("friend.invite_email"), {"email": target.email})
            registered = EmailSendLog.objects.filter(sender=self.inviter.profile).count()
            self.client.post(reverse("friend.invite_email"), {"email": "unregistered@example.com"})
        self.assertEqual(registered, 1)
        self.assertEqual(EmailSendLog.objects.filter(sender=self.inviter.profile).count(), 2)

    def test_the_join_email_is_sent_after_the_request_not_inside_it(self) -> None:
        with (
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            patch("django.core.mail.EmailMultiAlternatives.send") as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(reverse("friend.invite_email"), {"email": "unregistered@example.com"})
        self.assertLess(response.status_code, 400)
        send.assert_not_called()
        self.assertEqual(enqueue.call_count, 1)


class UnverifiedPrimaryAddressTests(TestCase):
    """An account that set someone else's address as its primary, without verifying it, is not its owner."""

    def test_the_mailbox_gets_the_invitation_not_the_squatter(self) -> None:
        inviter = baker.make(User, username="asker", email="asker@example.com")
        squatter = make_invitable_user(username="squatter", email="squatter@example.com", is_active=True)
        squatter.email = "victim@example.com"
        squatter.save(update_fields=["email"])
        self.client.force_login(inviter)
        with patch("django.core.mail.EmailMultiAlternatives.send"):
            self.client.post(reverse("friend.invite_email"), {"email": "victim@example.com"})
        self.assertFalse(Friendship.objects.filter(to_profile=squatter.profile).exists())
        self.assertTrue(FriendInvitation.objects.filter(inviter=inviter.profile, email="victim@example.com").exists())


class InviteRequestIsIndependentOfTheAddressTests(TestCase):
    """Until the delivery task runs, an account's address and an unregistered one leave the same trace."""

    def test_the_request_leaves_the_same_state_and_runs_the_same_queries(self) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from urbanlens.dashboard.services.social.friendship import invite_by_email

        target = make_invitable_user(username="registered", email="registered@example.com", is_active=True)

        def run(email: str) -> int:
            inviter = baker.make(User, email=f"inviter-{email}")
            with (
                patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
                self.captureOnCommitCallbacks(execute=True),
                CaptureQueriesContext(connection) as ctx,
            ):
                invite_by_email(inviter.profile, email, url_builder=lambda path: f"https://x.test{path}")
            self.assertTrue(FriendInvitation.objects.filter(inviter=inviter.profile).exists())
            self.assertFalse(Friendship.objects.filter(from_profile=inviter.profile).exists())
            return len(ctx.captured_queries)

        self.assertEqual(run(target.email), run("unregistered@example.com"))


class SenderSeesTheSameAfterDeliveryTests(TestCase):
    """What the sender sees once delivery has run, and over time, cannot depend on the address having an account."""

    def setUp(self) -> None:
        from urbanlens.dashboard.models.site_settings import SiteSettings

        settings = SiteSettings.get_current()
        settings.email_limit_per_hour = 0
        settings.save()
        self.registered = make_invitable_user(username="registered", email="registered@example.com", is_active=True)

    def _invite(self, inviter: User, email: str) -> None:
        self.client.force_login(inviter)
        with (
            patch("django.core.mail.EmailMultiAlternatives.send"),
            tasks_run_inline(deliver_friend_invitation),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(reverse("friend.invite_email"), {"email": email})

    def _widget(self, inviter: User) -> dict:
        from urbanlens.dashboard.controllers.friendship import _friend_list_ctx

        return _friend_list_ctx(inviter.profile, inviter.profile)

    def _each_kind(self, run) -> dict[str, object]:
        results = {}
        for label, email in (("registered", self.registered.email), ("unregistered", "nobody-here@example.com")):
            inviter = baker.make(User, email=f"sender-{label}@example.com")
            results[label] = run(inviter, email)
        return results

    def test_the_pending_entry_and_its_cancel_token_survive_delivery(self) -> None:
        def run(inviter: User, email: str) -> tuple[int, bool]:
            self.client.force_login(inviter)
            with (
                patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
                self.captureOnCommitCallbacks(execute=True),
            ):
                self.client.post(reverse("friend.invite_email"), {"email": email})
            before = [entry["cancel_token"] for entry in self._widget(inviter)["outgoing_pending"]]
            invitation = FriendInvitation.objects.get(inviter=inviter.profile)
            from urbanlens.dashboard.services.social.friend_invitations import deliver

            with patch("django.core.mail.EmailMultiAlternatives.send"):
                deliver(invitation.pk, "https://x.test/", send_join_email=True)
            after = [entry["cancel_token"] for entry in self._widget(inviter)["outgoing_pending"]]
            return len(after), before == after

        results = self._each_kind(run)
        self.assertEqual(results["registered"], results["unregistered"])
        self.assertEqual(results["registered"], (1, True))

    def test_inviting_twice_leaves_one_entry_either_way(self) -> None:
        def run(inviter: User, email: str) -> int:
            self._invite(inviter, email)
            self._invite(inviter, email)
            return self._widget(inviter)["outgoing_pending_count"]

        self.assertEqual(self._each_kind(run), {"registered": 1, "unregistered": 1})

    def test_the_entry_expires_the_same_way_either_way(self) -> None:
        import datetime

        from django.utils import timezone

        def run(inviter: User, email: str) -> int:
            self._invite(inviter, email)
            FriendInvitation.objects.filter(inviter=inviter.profile).update(
                expires_at=timezone.now() - datetime.timedelta(minutes=1)
            )
            return self._widget(inviter)["outgoing_pending_count"]

        self.assertEqual(self._each_kind(run), {"registered": 0, "unregistered": 0})

    def test_the_api_friend_list_names_no_one(self) -> None:
        def run(inviter: User, email: str) -> bool:
            self._invite(inviter, email)
            return Friendship.objects.filter(from_profile=inviter.profile).exists()

        self.assertEqual(self._each_kind(run), {"registered": False, "unregistered": False})

    def test_the_registered_invitee_answers_on_the_invitation_page(self) -> None:
        inviter = baker.make(User, email="sender@example.com")
        self._invite(inviter, self.registered.email)
        invitation = FriendInvitation.objects.get(inviter=inviter.profile)
        self.assertEqual(invitation.invitee_id, self.registered.profile.pk)
        self.client.force_login(self.registered)
        page = self.client.get(reverse("friend.invitation", kwargs={"token": invitation.token}))
        self.assertContains(page, "Accept")
        self.client.post(reverse("friend.invitation.answer", kwargs={"token": invitation.token}), {"answer": "accept"})
        self.assertEqual(
            Friendship.objects.all().between(inviter.profile, self.registered.profile).status, FriendshipStatus.ACCEPTED
        )

    def test_a_decline_is_not_shown_to_the_sender(self) -> None:
        inviter = baker.make(User, email="sender@example.com")
        self._invite(inviter, self.registered.email)
        invitation = FriendInvitation.objects.get(inviter=inviter.profile)
        self.client.force_login(self.registered)
        self.client.post(reverse("friend.invitation.answer", kwargs={"token": invitation.token}), {"answer": "decline"})
        self.assertEqual(self._widget(inviter)["outgoing_pending_count"], 1)
        self.assertFalse(Friendship.objects.all().between(inviter.profile, self.registered.profile))

    def test_an_unregistered_address_can_decline_without_an_account(self) -> None:
        inviter = baker.make(User, email="sender@example.com")
        self._invite(inviter, "nobody-here@example.com")
        invitation = FriendInvitation.objects.get(inviter=inviter.profile)
        self.client.logout()
        self.assertContains(
            self.client.get(reverse("friend.invitation", kwargs={"token": invitation.token})), "Decline"
        )
        self.client.post(reverse("friend.invitation.answer", kwargs={"token": invitation.token}), {"answer": "decline"})
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.declined_at)

    def test_a_withdrawn_invitation_is_never_delivered(self) -> None:
        from urbanlens.dashboard.services.social.friend_invitations import deliver

        inviter = baker.make(User, email="sender@example.com")
        self.client.force_login(inviter)
        with (
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(reverse("friend.invite_email"), {"email": self.registered.email})
        invitation = FriendInvitation.objects.get(inviter=inviter.profile)
        invitation.delete()
        deliver(invitation.pk, "https://x.test/", send_join_email=True)
        self.assertFalse(NotificationLog.objects.filter(profile=self.registered.profile).exists())


class JoinEmailOnceTests(TestCase):
    """The one join email per address is used up only by a send that happened."""

    def test_a_failed_send_is_retried_by_the_next_invite(self) -> None:
        import smtplib

        inviter = baker.make(User, email="sender@example.com")
        self.client.force_login(inviter)
        with (
            patch("django.core.mail.EmailMultiAlternatives.send", side_effect=smtplib.SMTPException("down")),
            tasks_run_inline(deliver_friend_invitation),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(reverse("friend.invite_email"), {"email": "nobody-here@example.com"})
        with (
            patch("django.core.mail.EmailMultiAlternatives.send") as send,
            tasks_run_inline(deliver_friend_invitation),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(reverse("friend.invite_email"), {"email": "nobody-here@example.com"})
        self.assertEqual(send.call_count, 1)
        from urbanlens.dashboard.models.email_log import EmailSendLog

        self.assertEqual(EmailSendLog.objects.filter(sender=inviter.profile).count(), 1)


class ProviderVerifiedEmailTests(TestCase):
    """A social sign-in whose provider verified the address counts as owning it."""

    def test_a_verified_provider_address_is_recorded(self) -> None:
        from urbanlens.dashboard.services.social_auth.pipeline import record_provider_verified_email

        user = baker.make(User, email="sso@example.com")
        record_provider_verified_email(None, user, {"email": "sso@example.com", "email_verified": True})
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.verified_primary_email, "sso@example.com")

    def test_an_unverified_or_different_provider_address_is_not(self) -> None:
        from urbanlens.dashboard.services.social_auth.pipeline import record_provider_verified_email

        user = baker.make(User, email="sso@example.com")
        record_provider_verified_email(None, user, {"email": "sso@example.com", "verified": False})
        record_provider_verified_email(None, user, {"email": "other@example.com", "email_verified": True})
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.verified_primary_email, "")
