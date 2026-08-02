"""External API: the friend-invite endpoint must not leak site membership.

Mirrors the internal-surface test in ``test_friend_invite_privacy.py``, but
raises the bar: rather than comparing two cases, this compares all four
outcomes the endpoint can reach without a validation error, and asserts the
status code, the response body *and* the header names are identical across
every one of them.

That strictness is the point. ``POST /friend-invites/`` takes an arbitrary
email address and is reachable by anyone holding an API key. If any observable
part of the response varied by whether the address belonged to an account, the
endpoint would be a membership-enumeration oracle: try addresses one at a
time, diff the responses, harvest the site's user list.

The four cases:

1. the address belongs to a registered account that accepts requests;
2. the address belongs to nobody;
3. the address belongs to a registered account whose privacy settings refuse
   the request (nothing is created);
4. the address belongs to nobody and the outbound mail send raises.
"""

from __future__ import annotations

import smtplib
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKeyScope
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.meta import VisibilityChoice
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    """Build the Authorization header kwargs for the test client.

    Args:
        raw_key: The plaintext API key.

    Returns:
        Kwargs to splat into a test-client call.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class ExternalInvitePrivacyTests(TestCase):
    """The invite response must be byte-identical across every non-validation outcome."""

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to site admin
        self.url = reverse("external_api:friend_invites")

    def _inviter_key(self) -> str:
        """Create a fresh inviter and return an API key granting social:write.

        A distinct inviter per case keeps the shared per-user outbound-email
        budget from coupling the four cases together - a rate limit tripped by
        case 3 would otherwise change case 4's response for reasons that have
        nothing to do with the property under test.

        Returns:
            The plaintext API key.
        """
        user = baker.make(User, email=f"inviter{baker.random_gen.gen_integer(1, 10**9)}@example.com")
        api_key, raw_key = generate_api_key(user, "Test")
        api_key.scopes = [ApiKeyScope.SOCIAL_WRITE.value]
        api_key.save(update_fields=["scopes"])
        return raw_key

    def _invite(self, raw_key: str, email: str):
        """POST one invitation.

        Args:
            raw_key: The caller's API key.
            email: The address to invite.

        Returns:
            The HTTP response.
        """
        return self.client.post(
            self.url,
            {"email": email},
            content_type="application/json",
            **_bearer(raw_key),
        )

    def _case_registered_open(self):
        """Case 1: a real account that accepts friend requests."""
        target = baker.make(User, email="open-target@example.com", is_active=True)
        profile = Profile.objects.get(user=target)
        profile.friend_request_visibility = VisibilityChoice.ANYONE
        profile.save()
        return self._invite(self._inviter_key(), target.email)

    def _case_unregistered(self):
        """Case 2: nobody owns the address."""
        return self._invite(self._inviter_key(), "nobody-at-all@example.com")

    def _case_registered_but_refused(self):
        """Case 3: a real account whose privacy settings refuse the request."""
        target = baker.make(User, email="closed-target@example.com", is_active=True)
        profile = Profile.objects.get(user=target)
        profile.friend_request_visibility = VisibilityChoice.NO_ONE
        profile.save()
        return self._invite(self._inviter_key(), target.email)

    def _case_send_fails(self):
        """Case 4: nobody owns the address and the mail send blows up."""
        # Patched on django.core.mail itself, not on the service module: the
        # service imports the class inside the function precisely so patches
        # here take effect (see invite_by_email's own comment).
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send",
            side_effect=smtplib.SMTPException("nope"),
        ):
            return self._invite(self._inviter_key(), "send-explodes@example.com")

    def test_all_four_outcomes_are_indistinguishable(self) -> None:
        """Status, body bytes and header names must match across all four cases."""
        responses = {
            "registered_open": self._case_registered_open(),
            "unregistered": self._case_unregistered(),
            "registered_refused": self._case_registered_but_refused(),
            "send_failed": self._case_send_fails(),
        }

        baseline_name, baseline = next(iter(responses.items()))
        for name, response in responses.items():
            with self.subTest(case=name):
                self.assertEqual(response.status_code, baseline.status_code, f"{name} vs {baseline_name}: status differs")
                self.assertEqual(response.content, baseline.content, f"{name} vs {baseline_name}: body differs")
                self.assertEqual(sorted(response.headers), sorted(baseline.headers), f"{name} vs {baseline_name}: header names differ")

    def test_response_is_exactly_the_invariant_sent_document(self) -> None:
        response = self._case_unregistered()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"result": "sent"})

    def test_refused_target_really_gets_no_friendship_row(self) -> None:
        """The identical response must not be masking an actual side effect."""
        target = baker.make(User, email="closed2@example.com", is_active=True)
        profile = Profile.objects.get(user=target)
        profile.friend_request_visibility = VisibilityChoice.NO_ONE
        profile.save()

        self._invite(self._inviter_key(), target.email)

        self.assertFalse(Friendship.objects.filter(to_profile=profile).exists())

    def test_open_target_really_does_get_a_friendship_row(self) -> None:
        """Conversely, the identical response is not hiding a no-op either."""
        target = baker.make(User, email="open2@example.com", is_active=True)
        profile = Profile.objects.get(user=target)
        profile.friend_request_visibility = VisibilityChoice.ANYONE
        profile.save()

        self._invite(self._inviter_key(), target.email)

        self.assertTrue(Friendship.objects.filter(to_profile=profile).exists())

    def test_unregistered_address_creates_a_pending_invitation(self) -> None:
        self._invite(self._inviter_key(), "future-member@example.com")
        self.assertTrue(FriendInvitation.objects.filter(email="future-member@example.com").exists())

    def test_malformed_email_is_the_one_permitted_difference(self) -> None:
        """A 400 here is fine: it depends only on what the caller submitted."""
        response = self._invite(self._inviter_key(), "not-an-email")
        self.assertEqual(response.status_code, 400)

    def test_subscription_role_is_not_accepted_from_an_api_key(self) -> None:
        """The site-admin escalation path must not exist on this surface at all."""
        raw_key = self._inviter_key()
        response = self.client.post(
            self.url,
            {"email": "someone-else@example.com", "subscription_role": "pro"},
            content_type="application/json",
            **_bearer(raw_key),
        )
        self.assertEqual(response.status_code, 200)
        # The extra key is ignored, not honoured - no grant was staged.
        from urbanlens.dashboard.models.subscriptions import PendingSubscriptionGrant

        self.assertFalse(PendingSubscriptionGrant.objects.exists())
